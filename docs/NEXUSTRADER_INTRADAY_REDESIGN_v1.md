# NexusTrader Intraday Redesign — System Architecture Document

**Version:** 1.0
**Date:** 2026-04-05
**Author:** System Architecture Review
**Status:** Design Complete — Pending Audit
**Classification:** Internal Engineering

---

## 1. Executive Summary

This document specifies the complete architectural redesign of NexusTrader from a swing-oriented trading system (30m/1h/4h timeframes, 5-minute scan cadence, 1–3 trades/day) into a high-performance intraday crypto day trading platform targeting 15–30 trades/day across ~20 assets with 10–90 minute holding periods.

The redesign addresses five structural deficiencies in the current system: (1) timeframes too slow for intraday momentum capture, (2) scan cadence (5 minutes) creating 150–300 second signal lag, (3) REST-only data fetching introducing cumulative latency across 20 assets, (4) strategy logic designed for multi-hour holds that cannot capture sub-hour price movements, and (5) Qt GUI thread coupling that constrains execution pipeline throughput.

The target system operates on a three-layer timeframe hierarchy (1m/3m execution → 5m/15m setup → 15m/1h bias), replaces swing strategies with five purpose-built intraday models, decouples the execution engine from the Qt GUI into a headless core, and transitions data ingestion to WebSocket-first with REST fallback. The design maintains all existing risk controls while adapting them for higher trade frequency, targets PF > 1.3 with realistic fees (0.04%/side), and provides a phased migration path that preserves system stability throughout.

**Deployment environment:** Windows + NVIDIA RTX 4070, residential internet, Singapore VPN (~50–150ms round-trip to Bybit). All latency budgets and architectural decisions account for this constraint.

---

## 2. Current System Limitations

### 2.1 Timeframe Architecture

The current system operates on 30m (primary), 1h (SLC context), and 4h (PBL confirmation, MTF gate). At 30m resolution, the minimum theoretical signal-to-entry latency is 30 minutes — the system cannot react to any price movement until the current 30m candle closes. In practice, with the 5-minute scan cadence, worst-case latency is 35 minutes (candle closes at T+0, next scan fires at T+5m).

For intraday trading targeting 10–90 minute holds, a 30-minute entry resolution means the system often enters trades that have already consumed 30–60% of the move's duration. This is structurally incompatible with the target holding period.

### 2.2 Scan Cadence

`AssetScanner` runs on a candle-boundary-aligned timer. Current Phase 1 fetches OHLCV for all symbols concurrently (20 workers), Phase 2 computes indicators and runs all sub-models per symbol, and Phase 3 finalizes through `RiskGate`. The entire pipeline executes every 5 minutes. Even with the 20-worker parallelism, this creates a fundamental information delay: market-moving events (sudden breakouts, liquidation cascades, VWAP reclaims) that occur between scans are invisible to the system until the next cycle.

### 2.3 Data Ingestion

`ExchangeManager` uses synchronous REST calls via `ccxt.Exchange` for all data. WebSocket support exists (`get_ws_exchange()` via ccxt.pro) but is disabled (`websocket_enabled: false`) due to historical Qt thread safety issues — WS callbacks at 10Hz without throttle crashed the Qt event loop. Every scan cycle makes N × M REST calls (N symbols × M timeframes), each taking ~100ms. For 20 symbols × 3 timeframes, this is ~6 seconds of serial I/O even with concurrency.

### 2.4 Strategy Design

Both active strategies are swing-oriented:

**PullbackLong (PBL):** Requires 30m candle pullback to EMA50 within a 4h bull trend, confirmed by a rejection candle pattern. Minimum setup development time is 2–4 hours (4h trend establishment + 30m pullback formation). Backtest PF of 0.8995 in isolation — only profitable as part of the combined PBL+SLC system.

**SwingLowContinuation (SLC):** Requires 1h close below 10-bar swing low in a bear trend with ADX ≥ 28. Minimum setup time is 10+ hours (swing low formation over 10 hourly bars). Holding periods of 4–12 hours typical.

Neither strategy can produce the target 15–30 trades/day. Their combined backtest produced 1,745 trades over 4 years (1.2 trades/day across 3 assets). Scaling to 20 assets would yield ~8 trades/day — still below target and with holding periods far exceeding the 10–90 minute envelope.

### 2.5 Signal Pipeline Overhead

The `ConfluenceScorer` aggregates signals from all active sub-models plus the `OrchestratorEngine` meta-signal (12 AI agents). For intraday trading, the orchestrator adds latency (agents poll on 60s–21600s intervals) and the ensemble voting pattern assumes independent, slow-arriving signals — appropriate for swing trading where signal quality dominates speed, but counterproductive when speed of entry is itself a component of edge.

### 2.6 Qt Coupling

The current architecture routes all cross-thread communication through Qt signals with `QueuedConnection`. `BaseAgent` inherits `QThread`. `OrchestratorEngine` inherits `QObject`. `EventBus` emits Qt `Signal(object)` for UI subscribers. `PaperExecutor.on_tick()` must execute on the main thread. This coupling means the execution pipeline shares the Qt event loop with 20 GUI pages, creating priority inversion: a heavy UI render (e.g., Performance Analytics with 9 tabs) can delay tick processing.

---

## 3. Target System Architecture

### 3.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     HEADLESS EXECUTION CORE                        │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │ DataEngine   │───▶│ StrategyBus  │───▶│ ExecutionManager     │  │
│  │ (WS + REST)  │    │ (Setup→Trig) │    │ (Order routing)      │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
│         │                    │                      │               │
│         ▼                    ▼                      ▼               │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │ CandleBuilder│    │ RegimeEngine │    │ RiskController       │  │
│  │ (1m→5m→15m) │    │ (Fast regime)│    │ (Heat, DD, limits)   │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐  │
│  │              Pure-Python EventBus (no Qt dependency)          │  │
│  └──────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────────┐  │
│  │ AgentPool    │    │ CrashDefense │    │ PerformanceTracker   │  │
│  │ (4 retained) │    │ (unchanged)  │    │ (L1/L2 learning)     │  │
│  └──────────────┘    └──────────────┘    └──────────────────────┘  │
└─────────────────────────────────────────────────────────────────────┘
         ▲                                           │
         │ WebSocket API (localhost:8765)             │
         ▼                                           ▼
┌────────────────────┐                    ┌──────────────────────┐
│ Web Dashboard      │                    │ Qt GUI (optional)    │
│ (primary monitor)  │                    │ (legacy, read-only)  │
└────────────────────┘                    └──────────────────────┘
```

### 3.2 Core Design Principles

**Principle 1 — Headless-first execution.** The trading engine runs as a standalone Python process with zero Qt imports. All state is accessible via an internal WebSocket API. The Qt GUI (if running) connects as an external observer and can be attached/detached without affecting execution.

**Principle 2 — Event-driven, not poll-driven.** Price data arrives via WebSocket streams. Candle closes trigger strategy evaluation immediately — no timer-based polling. The system reacts to market events within the latency budget, not on an arbitrary schedule.

**Principle 3 — Hierarchical timeframe gating.** Every trade must satisfy three independent layers: (1) directional bias from 15m/1h, (2) structural setup from 5m/15m, and (3) precise entry trigger from 1m/3m. No layer can be bypassed. This prevents overtrading while enabling fast entries.

**Principle 4 — Capital efficiency through rotation.** With 10–90 minute holds and 20 assets, the system targets 60–80% capital deployment during active hours. Positions that exceed 2 hours without hitting TP are force-exited (time stop), freeing capital for fresh setups.

**Principle 5 — Preserve defense-in-depth risk controls.** The existing crash defense system (7-component scorer, 4-tier response), portfolio heat management, EV gate, and drawdown circuit breakers remain intact. They are adapted for higher frequency but not weakened.

### 3.3 Component Mapping (Current → Target)

| Current Component | Disposition | Target Component |
|---|---|---|
| `AssetScanner` (5m poll) | **Replace** | `DataEngine` + `CandleBuilder` (WS-driven) |
| `SignalGenerator` + sub-models | **Replace** | `StrategyBus` with 5 intraday models |
| `ConfluenceScorer` | **Adapt** | Simplified 2-stage scorer (Setup + Trigger) |
| `RiskGate` | **Adapt** | `RiskController` (same checks, tighter limits) |
| `PositionSizer` | **Adapt** | Reduced per-trade risk for higher frequency |
| `PaperExecutor` | **Adapt** | `ExecutionManager` (headless, no Qt) |
| `OrchestratorEngine` (12 agents) | **Reduce** | `AgentPool` (4 agents, async background) |
| `EventBus` (Qt-based) | **Replace** | Pure-Python `EventBus` (asyncio-native) |
| `HMM RegimeClassifier` | **Adapt** | `RegimeEngine` (faster refit, 5m resolution) |
| `CrashDefenseController` | **Retain** | Unchanged (already headless-compatible) |
| Qt GUI (20 pages) | **Demote** | Optional observer via WebSocket API |
| All 23 remaining agents | **Rationalize** | 4 agents retained (see Section 8) |

---

## 4. Timeframe Strategy

### 4.1 Three-Layer Hierarchy

| Layer | Timeframe | Role | Update Frequency | Lookback |
|---|---|---|---|---|
| **Bias** | 15m + 1h | Directional trend filter | On 15m/1h candle close | 100 bars (15m = 25h, 1h = 100h) |
| **Setup** | 5m + 15m | Structural pattern identification | On 5m candle close | 60 bars (5h) |
| **Trigger** | 1m + 3m | Precise entry timing | On 1m candle close | 30 bars (30min) |

### 4.2 Justification for Each Timeframe

**1-minute (Trigger layer):** The atomic execution timeframe. All entries and exits are evaluated on 1m candle closes. At 1m resolution, the maximum signal-to-entry latency is 60 seconds — a 35× improvement over the current 35-minute worst case. The 1m timeframe captures micro-structure (order flow imbalance manifesting as rapid price movement, VWAP touches, consolidation breakouts) that is invisible at 5m+. The risk: 1m candles are noisy. This is mitigated by requiring 5m/15m setup qualification before any 1m trigger is evaluated.

**3-minute (Trigger layer, selective):** Used by the Micro Pullback Continuation strategy specifically. 3m candles smooth out the noise of 1m while retaining enough granularity for momentum continuation entries. The 3m timeframe is derived from 1m candles (every 3rd close), not fetched independently.

**5-minute (Setup layer):** The primary setup identification timeframe. 5m candles provide sufficient structure to identify consolidation ranges, pullback patterns, volume climax events, and VWAP interactions without the noise of 1m. A 5m setup window of 60 bars covers 5 hours — enough to capture an intraday session's structure. 5m is also the highest resolution where OHLCV indicators (EMA, Bollinger Bands, RSI) produce statistically meaningful signals in crypto markets.

**15-minute (Setup + Bias overlap):** Bridges setup and bias layers. Used for: (a) higher-timeframe structure confirmation (does the 5m breakout align with 15m trend?), (b) regime classification (the fastest timeframe where HMM regime states are stable enough to be actionable), and (c) VWAP slope calculation (15m VWAP slope is a reliable intraday trend indicator).

**1-hour (Bias layer):** Provides the macro directional bias. The 1h EMA20/EMA50 relationship, ADX, and regime classification establish whether the system should favor longs, shorts, or stand aside for a given asset. The 1h timeframe is updated once per hour — slow enough to be stable, fast enough to adapt within a trading day.

**4-hour (Removed from active pipeline):** The current 4h MTF confirmation gate is removed. Rationale: 4h regime changes are too slow for intraday trading. A 4h bear trend starting at 08:00 UTC would suppress all long entries until 12:00 UTC at earliest — missing the majority of intraday reversals. The 1h bias layer provides sufficient directional gating. The 4h timeframe may be consulted at session open for daily context but does not gate any trades.

### 4.3 Candle Derivation Strategy

To minimize API calls and ensure consistency, all timeframes above 1m are derived from 1m candles:

```
1m (raw from WebSocket) → aggregate to:
  ├─ 3m  (every 3 × 1m candles)
  ├─ 5m  (every 5 × 1m candles)
  ├─ 15m (every 15 × 1m candles)
  └─ 1h  (every 60 × 1m candles)
```

This guarantees OHLCV consistency across timeframes (no discrepancies between exchange-provided 5m candles and locally-derived ones) and eliminates the need to fetch multiple timeframes via REST.

### 4.4 Warm-Up Requirements

On system startup or after data gap:

| Timeframe | Bars Needed | Duration | Source |
|---|---|---|---|
| 1m | 200 | 3.3 hours | REST backfill then WS stream |
| 5m | 100 | 8.3 hours | Derived from 1m |
| 15m | 100 | 25 hours | Derived from 1m (or REST if >24h gap) |
| 1h | 100 | 100 hours | REST backfill (4.2 days) |

The system enters a "warm-up" state on startup. During warm-up, the bias layer (1h) is populated via REST backfill. The system is trade-ready once 1h warm-up completes (~4 minutes at 20 symbols × 1 REST call each). 1m/5m/15m warm-up accumulates in real-time from WS stream, with the trigger and setup layers becoming active progressively (1m active after 200 minutes = 3.3h, 5m after 8.3h). During partial warm-up, only strategies with sufficient data are active.

---

## 5. Strategy Redesign

### 5.1 Existing Strategy Disposition

**PullbackLong (PBL) — REMOVE from active pipeline.** PBL requires 4h trend confirmation and 30m pullback formation. Its minimum setup time (2–4 hours) and isolated PF of 0.8995 make it unsuitable for intraday trading. The concept of "pullback to EMA in a trend" is preserved in the new Micro Pullback Continuation strategy but adapted to 5m/15m timeframes.

**SwingLowContinuation (SLC) — REMOVE from active pipeline.** SLC requires 1h swing low formation over 10 bars (10 hours minimum). This is fundamentally incompatible with the 10–90 minute target hold time. The concept of "continuation after structure break" is preserved in the new Momentum Expansion strategy.

**MomentumBreakout — ADAPT.** The existing model's core logic (price breaks N-bar high/low with volume confirmation) is sound for intraday use. It needs timeframe adaptation (20-bar range at 5m = 100-minute range, appropriate for intraday) and tighter stops.

**All other models (Trend, FundingRate, Sentiment, MeanReversion, VWAP, OrderBook, LiquiditySweep, Donchian) — DISABLE.** These either target swing timeframes, have negative expectancy (PF < 1.0), or rely on agent data that adds latency without demonstrable edge. FundingRate and Sentiment are repurposed as lightweight agent-based bias modifiers (see Section 8) rather than standalone signal generators.

### 5.2 New Intraday Strategy Suite

Five strategies, each targeting a distinct market microstructure condition. Combined, they cover trending, ranging, mean-reverting, and breakout regimes.

---

#### Strategy 1: Momentum Expansion (MX)

**Market condition:** Directional conviction increasing — price breaking out of consolidation with volume confirmation.

**Timeframe roles:**
- Bias (15m/1h): EMA20 > EMA50 (longs) or EMA20 < EMA50 (shorts). ADX > 20 on 15m.
- Setup (5m): Price consolidates in a range ≤ 1.5× ATR(14) width for ≥ 6 bars (30 minutes minimum). Bollinger Band width percentile < 30th over 50 bars (compression).
- Trigger (1m): Candle closes beyond range boundary + volume on breakout bar > 1.8× 20-bar volume MA. RSI on 1m confirms direction (> 55 for long, < 45 for short).

**Entry logic:**
```
LONG entry:
  1. 1h EMA20 > EMA50 (bias: bullish)
  2. 15m ADX > 20 (trend present)
  3. 5m: identify consolidation range (high/low of last 6+ bars, width ≤ 1.5 × ATR14)
  4. 5m: BB width percentile < 30 (squeeze detected)
  5. 1m: close > range_high AND volume > 1.8 × vol_ma20
  6. 1m: RSI(14) > 55
  → Entry at next 1m candle open (no same-bar fill)

SHORT entry: mirror conditions with inverted bias and trigger thresholds.
```

**Exit logic:**
- Stop loss: 1.2 × ATR(14) on 5m below entry (for longs). Placed at the consolidation range midpoint if that is tighter.
- Take profit: 2.5 × risk distance. Partial exit (50%) at 1.5R, remainder trails at 1.0 × ATR(14) on 5m.
- Time stop: 90 minutes maximum. Force exit if neither SL nor TP hit.
- Breakeven: Move SL to entry + 0.1× ATR after +1R.

**Expected characteristics:**
- Win rate: 45–55% (breakout strategies have moderate WR but strong winners)
- Average R: +1.4R (due to 2.5R target with 50% partial)
- PF target: 1.4–1.8
- Trade frequency: 3–6/day across 20 assets (consolidation→breakout is common in crypto)
- Typical hold: 15–60 minutes

**Indicator requirements:** EMA(20), EMA(50), ADX(14), ATR(14), Bollinger Bands(20,2), RSI(14), Volume MA(20), range detection (rolling N-bar high/low).

---

#### Strategy 2: VWAP Reclaim/Rejection (VR)

**Market condition:** Price interacting with VWAP — the volume-weighted average price acts as institutional fair value anchor. Reclaiming VWAP after a deviation signals mean reversion; rejecting at VWAP signals continuation.

**Timeframe roles:**
- Bias (15m/1h): Determines whether to trade reclaims (counter-trend, ranging regime) or rejections (with-trend, trending regime).
- Setup (5m): Price deviates from VWAP by > 0.5× ATR, then returns to within 0.15× ATR of VWAP. Volume on approach bars is declining (exhaustion).
- Trigger (1m): Rejection candle at VWAP (wick > 2× body, close in direction of trade). OR: reclaim candle (close crosses VWAP with volume > 1.3× average).

**Entry logic:**
```
VWAP RECLAIM (long):
  1. 15m regime: ranging or accumulation (NOT strong bear)
  2. 5m: price was below VWAP by > 0.5 × ATR14
  3. 5m: price returns to within 0.15 × ATR14 of VWAP
  4. 5m: volume declining on last 3 approach bars (exhaustion)
  5. 1m: close > VWAP AND volume > 1.3 × vol_ma20
  → Entry at next 1m candle open

VWAP REJECTION (short, with-trend):
  1. 1h EMA20 < EMA50 (bias: bearish)
  2. 5m: price rallies to VWAP from below (within 0.15 × ATR14)
  3. 1m: rejection candle (upper wick > 2 × body, close below VWAP)
  4. 1m: RSI < 50
  → Entry at next 1m candle open
```

**Exit logic:**
- Stop: 0.8 × ATR(14) on 5m beyond VWAP (opposite side of trade). Tight stop because VWAP interaction is a high-conviction level.
- Target: 2.0 × risk distance (reclaim) or 1.5 × risk distance (rejection, which is higher WR).
- Time stop: 60 minutes.
- VWAP cross invalidation: if price crosses VWAP against the trade by > 0.3 × ATR, exit immediately.

**Expected characteristics:**
- Win rate: 55–62% (VWAP is a well-defined level with mean-reverting properties)
- Average R: +1.0R (tighter targets compensated by higher WR)
- PF target: 1.3–1.6
- Trade frequency: 4–8/day across 20 assets (VWAP interactions are frequent)
- Typical hold: 10–45 minutes

**Indicator requirements:** Session VWAP (cumulative from UTC 00:00 or exchange session open), ATR(14), RSI(14), Volume MA(20), rejection candle pattern detection.

---

#### Strategy 3: Micro Pullback Continuation (MPC)

**Market condition:** Strong intraday trend with brief pullback — the "buy the dip in a trend" pattern adapted to 5m timeframes.

**Timeframe roles:**
- Bias (15m/1h): Strong trend — ADX > 25 on 15m, EMA9 > EMA21 > EMA50 on 15m (longs).
- Setup (5m): Price pulls back to 5m EMA9 or EMA21 (within 0.3 × ATR of EMA). Pullback is 2–5 bars (10–25 minutes). RSI retreats from overbought but stays > 40 (for longs).
- Trigger (1m/3m): Reversal candle pattern at EMA support. Specifically: 3m candle where close > open (bullish), lower wick > body, and close > previous bar high. Volume on trigger bar > 1.2× average.

**Entry logic:**
```
LONG:
  1. 15m: ADX > 25 AND EMA9 > EMA21 > EMA50 (strong uptrend)
  2. 1h: EMA20 > EMA50 (higher TF confirmation)
  3. 5m: close within 0.3 × ATR14 of EMA9 or EMA21
  4. 5m: RSI(14) > 40 AND RSI was > 60 within last 8 bars (pullback from strength)
  5. 5m: pullback duration 2–5 bars (not a reversal, which would be >5 bars)
  6. 3m: reversal candle (close > open, lower_wick > body, close > prev_high)
  7. 3m: volume > 1.2 × vol_ma20
  → Entry at next 1m candle open

SHORT: mirror with inverted EMA ordering, RSI < 60, upper wick pattern.
```

**Exit logic:**
- Stop: Below the pullback low (for longs) or 1.0 × ATR(14) on 5m, whichever is tighter.
- Target: 2.0 × risk. No partial exit — MPC targets are achieved quickly in strong trends.
- Time stop: 45 minutes. Strong trends either continue or die quickly.
- Trend invalidation: if 5m EMA9 crosses below EMA21, exit immediately.

**Expected characteristics:**
- Win rate: 50–58% (trend continuation is a high-base-rate setup)
- Average R: +1.3R
- PF target: 1.4–1.7
- Trade frequency: 4–8/day across 20 assets (pullbacks in trends are the most common tradeable pattern)
- Typical hold: 15–45 minutes

**Indicator requirements:** EMA(9), EMA(21), EMA(50), ADX(14), ATR(14), RSI(14), Volume MA(20), pullback bar counter, reversal candle pattern.

---

#### Strategy 4: Range Breakout Reclaim (RBR)

**Market condition:** Price breaks out of a defined range, briefly retests the breakout level, then continues — capturing the "retest and go" pattern that has higher WR than raw breakouts.

**Timeframe roles:**
- Bias (15m/1h): No strong counter-trend (ADX < 35 on 1h in opposing direction — avoids fighting strong trends).
- Setup (5m): Define range over ≥ 12 bars (1 hour minimum). Range width: 0.8–2.5× ATR(14). Price breaks range boundary (close beyond range). Then: price retests the broken boundary within 6 bars (30 minutes), touching or penetrating it by < 0.3 × ATR.
- Trigger (1m): After retest, 1m candle closes in breakout direction with volume > 1.5× average. Price is back beyond the range boundary.

**Entry logic:**
```
LONG (breakout above range, retest of range high):
  1. 1h: NOT (EMA20 < EMA50 AND ADX > 35) — no strong downtrend
  2. 5m: range identified (12+ bars, width 0.8–2.5 × ATR14)
  3. 5m: close > range_high (initial breakout)
  4. 5m: within 6 bars, price returns to range_high ± 0.3 × ATR (retest)
  5. 1m: close > range_high AND volume > 1.5 × vol_ma20
  → Entry at next 1m candle open

SHORT: mirror for breakdown below range low with retest.
```

**Exit logic:**
- Stop: Inside the range (range_high − 0.5× range_width for longs). This gives the trade room to retest but invalidates if it falls back into the range.
- Target: Range width projected from breakout level (measured move). 1.5× range width for first target.
- Partial: 50% at 1.0× range width, remainder at 1.5× or trail.
- Time stop: 75 minutes.

**Expected characteristics:**
- Win rate: 52–58% (retest confirms the breakout, filtering false breaks)
- Average R: +1.2R
- PF target: 1.3–1.5
- Trade frequency: 2–4/day across 20 assets (retested breakouts are less frequent but higher quality)
- Typical hold: 20–75 minutes

**Indicator requirements:** Range detection (N-bar high/low with width filter), breakout detection, retest detection (price returning to level within tolerance), ATR(14), Volume MA(20).

---

#### Strategy 5: Liquidity Sweep Reclaim (LSR)

**Market condition:** Price briefly spikes through a known liquidity level (previous high/low, round number, previous session extreme), triggering stop-loss orders, then rapidly reverses — the "stop hunt and reverse" pattern.

**Timeframe roles:**
- Bias (15m/1h): Not used as hard gate — LSR fires in any regime because stop hunts occur regardless of trend. However, regime modifies target size (trending regime: smaller target as price may continue; ranging: larger target).
- Setup (5m): Identify liquidity levels: previous session high/low, previous day high/low, swing highs/lows within last 50 bars. Price approaches within 0.5× ATR of a liquidity level.
- Trigger (1m): Price spikes through the level (wick extends ≥ 0.3× ATR beyond) but closes back inside (close on the "safe" side of the level). Volume spike > 2.0× average on the sweep bar (stop orders being hit). RSI divergence: price makes new extreme but RSI does not (for 5m RSI).

**Entry logic:**
```
LONG (sweep below support, reclaim):
  1. 5m: identify support level (prev session low, swing low, etc.)
  2. 1m: bar's low < support_level - 0.3 × ATR14 (price swept through)
  3. 1m: close > support_level (price reclaimed above support)
  4. 1m: volume > 2.0 × vol_ma20 (stop orders triggered = volume spike)
  5. 5m: RSI not at new low while price is at new low (bullish divergence, optional boost)
  → Entry at next 1m candle open

SHORT: mirror for sweep above resistance, reclaim below.
```

**Exit logic:**
- Stop: Below the sweep low (the wick extreme) + 0.2× ATR buffer. This is the invalidation — if price sweeps and continues, the pattern failed.
- Target: 2.0× risk (ranging regime) or 1.5× risk (trending regime against the trade direction).
- Time stop: 60 minutes. Liquidity reclaims either work quickly or fail.
- If price returns to the sweep level within 5 bars, exit immediately (second sweep = level is broken).

**Expected characteristics:**
- Win rate: 48–55% (lower WR but large R:R compensates)
- Average R: +1.5R
- PF target: 1.3–1.6
- Trade frequency: 2–5/day across 20 assets (liquidity sweeps require specific conditions)
- Typical hold: 10–60 minutes

**Indicator requirements:** Liquidity level identification (swing H/L detector, session extremes, round number proximity), ATR(14), RSI(14) for divergence, Volume MA(20), sweep pattern detection (wick beyond level + close inside).

---

### 5.3 Strategy Portfolio Summary

| Strategy | Regime Fit | WR Target | PF Target | Trades/Day | Hold Time |
|---|---|---|---|---|---|
| Momentum Expansion (MX) | Transition (compression→expansion) | 45–55% | 1.4–1.8 | 3–6 | 15–60m |
| VWAP Reclaim/Rejection (VR) | Ranging + Trending | 55–62% | 1.3–1.6 | 4–8 | 10–45m |
| Micro Pullback Cont. (MPC) | Strong trend | 50–58% | 1.4–1.7 | 4–8 | 15–45m |
| Range Breakout Reclaim (RBR) | Any (not strong counter) | 52–58% | 1.3–1.5 | 2–4 | 20–75m |
| Liquidity Sweep Reclaim (LSR) | Any | 48–55% | 1.3–1.6 | 2–5 | 10–60m |
| **Combined** | **Full coverage** | **50–57%** | **>1.3** | **15–31** | **10–90m** |

The combined portfolio targets 15–31 trades/day, which falls within the specified 15–30 normal day range. On high-volatility days, MX and LSR frequency increases (more breakouts and stop hunts), pushing toward 30–60 trades/day. On low-volatility days, MPC and VR carry the load with VWAP interactions in quieter markets.

### 5.4 Strategy Correlation Analysis

Strategy correlation must be low to prevent portfolio-level drawdown concentration:

- **MX vs RBR:** Moderate positive correlation (~0.4). Both are breakout-based but MX requires compression and RBR requires retest. Rarely fire on the same bar for the same asset.
- **VR vs MPC:** Low correlation (~0.15). VR is mean-reverting (price at VWAP); MPC is trend-continuing (price pulling back to EMA). They target different market states.
- **LSR vs all others:** Near zero correlation (~0.05). LSR fires on wick-based stop hunts, which are orthogonal to candle-close-based patterns.
- **MX vs MPC:** Low-moderate (~0.25). Both favor trending markets but MX enters on consolidation breaks while MPC enters on pullback reversals.

The portfolio is well-diversified across entry patterns, reducing the probability of correlated drawdowns.

---

## 6. Signal Pipeline Redesign

### 6.1 Two-Stage Architecture

The current single-stage pipeline (SignalGenerator → ConfluenceScorer → RiskGate) is replaced with an explicit two-stage model that separates setup qualification from execution triggering.

```
┌─────────────────────────────────────────────────────┐
│ STAGE A: Setup Qualification (runs on 5m close)     │
│                                                      │
│  For each asset:                                     │
│   1. Update 5m/15m indicators                        │
│   2. Update regime classification (15m)              │
│   3. Evaluate all 5 strategies' setup conditions     │
│   4. Produce SetupCandidate if conditions met        │
│   5. Store in ActiveSetups registry (TTL: 30 bars)   │
│                                                      │
│  Output: ActiveSetups[symbol] = list[SetupCandidate] │
│  SetupCandidate: {strategy, direction, bias_score,   │
│                   setup_level, invalidation_level,    │
│                   regime, timestamp, ttl}             │
└──────────────────────┬──────────────────────────────┘
                       │
                       ▼
┌─────────────────────────────────────────────────────┐
│ STAGE B: Execution Trigger (runs on 1m close)        │
│                                                      │
│  For each asset with ActiveSetups:                   │
│   1. Update 1m/3m indicators                         │
│   2. Check trigger conditions for each active setup  │
│   3. If trigger fires:                               │
│      a. Compute entry, SL, TP                        │
│      b. Run ConfluenceScore (simplified)             │
│      c. Run RiskGate validation                      │
│      d. If approved: submit to ExecutionManager      │
│   4. Expire stale setups (TTL exceeded)              │
│                                                      │
│  Output: OrderSubmission (or rejection + reason)     │
└─────────────────────────────────────────────────────┘
```

### 6.2 Stage A: Setup Qualification

Stage A runs every 5 minutes (on 5m candle close). This is computationally heavier — it evaluates all 5 strategies across all 20 assets, runs regime classification, and computes indicator values. Budget: < 2 seconds for all 20 assets (100ms per asset for indicators + strategy evaluation).

**Regime Classification:** The HMM regime classifier is adapted to 15m resolution. Instead of the current 6-state ensemble (which requires 200+ bars of 1h data), the intraday version uses a 4-state model: `trend_bull`, `trend_bear`, `ranging`, `volatile`. Training window: 100 bars of 15m data (25 hours). Refit frequency: every 4 hours (not every bar — HMM refit is expensive and states should be stable over multi-hour periods).

**Setup Scoring:** Each setup is assigned a `bias_score` (0.0–1.0) based on how many bias conditions are satisfied and how strongly. This replaces the full ConfluenceScorer at the setup stage. The bias_score weights:
- 1h trend alignment: 0.40
- 15m ADX strength: 0.20
- 15m regime confidence: 0.20
- Agent context modifier: 0.20 (from the 4 retained agents)

A setup requires `bias_score ≥ 0.35` to be registered. This is intentionally permissive — the trigger stage provides the precision filter.

**Setup TTL:** Each setup expires after 30 × (setup timeframe bars). For a 5m setup, TTL = 150 minutes. If the trigger doesn't fire within this window, the structural condition has likely changed and the setup is stale.

### 6.3 Stage B: Execution Trigger

Stage B runs every minute (on 1m candle close). It is computationally light — it only evaluates trigger conditions for assets that have active setups (typically 3–8 assets out of 20 at any given time, reducing the per-minute workload by 60–85%).

**Trigger Scoring:** When a trigger fires, a `trigger_score` (0.0–1.0) is computed based on:
- Volume confirmation strength: 0.30
- Price action quality (wick ratio, candle body size): 0.25
- RSI alignment: 0.15
- Setup bias_score passthrough: 0.30

The combined `trade_score = bias_score × 0.5 + trigger_score × 0.5` must exceed 0.40 for the trade to proceed to RiskGate. This is lower than the current 0.55 threshold because the two-stage filtering has already eliminated low-quality setups.

### 6.4 Confluence Simplification

The current ConfluenceScorer performs: direction dominance voting, adaptive L1/L2 weighting, correlation dampening, dynamic threshold adjustment, OI/liquidation modifiers, and MIL hard cap enforcement. For the intraday system, this is simplified:

**Retained:**
- Direction dominance check (but trivial — each strategy produces a single directional signal, no multi-model voting needed within a strategy)
- L1/L2 adaptive weighting (applies to per-strategy historical performance, not per-model)
- Dynamic threshold adjustment based on regime confidence

**Removed:**
- Multi-model confluence voting (replaced by strategy-internal logic that is already multi-indicator)
- Correlation dampening across models (no longer relevant with single-strategy entries)
- OI/liquidation modifiers (moved to agent bias in Stage A)
- MIL hard cap (no orchestrator meta-signal in the execution path)

**Cross-strategy conflict resolution:** If two strategies produce opposing signals for the same asset (e.g., MX signals long, VR signals short), the one with higher `trade_score` takes priority. If scores are within 0.05, neither fires (conflict = uncertainty).

### 6.5 Data Flow Diagram

```
WebSocket Tick Stream (20 symbols)
       │
       ▼
CandleBuilder (in-memory OHLCV ring buffers per symbol)
       │
       ├─── On 1m close ──────────────────────┐
       │                                        │
       ├─── On 5m close ──┐                    │
       │                    ▼                    ▼
       │          ┌──────────────────┐  ┌──────────────────┐
       │          │ STAGE A          │  │ STAGE B          │
       │          │ Setup Qualify    │  │ Trigger Check    │
       │          │ (all 20 assets)  │  │ (active setups)  │
       │          └────────┬─────────┘  └────────┬─────────┘
       │                   │                      │
       │                   ▼                      ▼
       │          ActiveSetups Registry    RiskGate Validation
       │                                          │
       │                                          ▼
       │                                  ExecutionManager
       │                                          │
       ├─── On 15m close ─── Regime Reclassify    │
       │                                          │
       └─── On 1h close ──── Bias Layer Update    │
                                                   ▼
                                           Position Tracking
                                           (on_tick per 1m)
```

---

## 7. Data & Scanning Architecture

### 7.1 WebSocket-First Data Ingestion

**Current state:** REST-only via `ExchangeManager.fetch_ohlcv()`. Each fetch is ~100ms. 20 symbols × scan = 2s minimum for raw data alone.

**Target state:** WebSocket streams for real-time 1m klines, REST for backfill and fallback only.

**Implementation:**

```
DataEngine
├── WSManager (ccxt.pro async)
│   ├── watch_ohlcv(symbol, "1m") × 20 symbols
│   ├── Reconnection: exponential backoff (1s, 2s, 4s, 8s, max 30s)
│   ├── Heartbeat: ping every 20s, reconnect if no pong within 10s
│   └── Thread: dedicated asyncio event loop (not Qt event loop)
│
├── CandleBuilder (pure Python, sync)
│   ├── Ring buffer per symbol per timeframe
│   │   ├── 1m: 300 bars (5 hours)
│   │   ├── 5m: 120 bars (10 hours) — derived from 1m
│   │   ├── 15m: 100 bars (25 hours) — derived from 1m
│   │   └── 1h: 100 bars (100 hours) — derived from 1m
│   ├── On WS kline close → update 1m → derive higher TFs → emit events
│   └── Gap detection: if 1m timestamp skips, fetch missing via REST
│
├── RESTBackfill (sync, startup only)
│   ├── On startup: fetch 1h × 100 bars for all 20 symbols (~20 REST calls)
│   ├── On reconnect: fetch missed 1m bars since last known timestamp
│   └── Rate limit: respect Bybit's 120 req/min (6 parallel, stagger)
│
└── IndicatorEngine (per-symbol, incremental)
    ├── Incremental EMA, RSI, ATR, BB, VWAP update on new bar
    ├── Full recalculate only on restart or data gap > 5 bars
    └── Pre-compute all indicators needed by all strategies at once
```

### 7.2 WebSocket Safety (Addressing Historical Issues)

The current system disabled WS due to Qt crashes at 10Hz without throttle. The redesign eliminates this by:

1. **No Qt in the data path.** The WS event loop runs in a dedicated `threading.Thread` with its own `asyncio` loop. No Qt signals, no QThread, no event loop sharing.
2. **Candle-close throttle.** Raw WS klines arrive every ~1s (Bybit's kline stream sends updates within the candle period). The `CandleBuilder` only emits events on candle close, reducing the effective event rate from ~1/s to 1/60s for 1m candles.
3. **Per-symbol rate limit.** If the WS stream sends more than 2 updates/second for any symbol (exchange anomaly), excess updates are dropped.

### 7.3 Latency Budget

| Stage | Budget | Mechanism |
|---|---|---|
| WS kline delivery | ~50–150ms | Network latency (Singapore VPN → Bybit) |
| CandleBuilder processing | < 1ms | In-memory ring buffer append |
| Indicator update (incremental) | < 5ms per symbol | Incremental EMA/RSI/ATR/BB |
| Stage B trigger evaluation | < 10ms per active setup | Simple conditional checks |
| RiskGate validation | < 5ms | Stateless check sequence |
| Order submission | ~50–150ms | REST order via `ccxt` |
| **Total signal-to-order** | **~120–330ms** | **WS latency dominates** |

This is a ~6,000× improvement over the current worst-case 35-minute latency. Even accounting for the residential internet + VPN overhead, the system reacts within 1 second of a candle close.

### 7.4 Scalability to 20 Assets

Bybit's WS API supports up to 10 subscriptions per connection. For 20 symbols × 1 stream (kline_1m), this requires 2 WS connections. The `WSManager` maintains 2–3 connections with automatic symbol distribution:

- Connection 1: Symbols 1–10 (kline_1m)
- Connection 2: Symbols 11–20 (kline_1m)
- Connection 3: (optional) Ticker streams for spread/volume monitoring

Each connection handles ~10 messages/second at peak. This is well within Bybit's WS limits (100 subscriptions per connection, 500 messages/second).

### 7.5 Data Gap Handling

| Scenario | Detection | Recovery |
|---|---|---|
| WS disconnect (< 5 min) | Heartbeat timeout | Reconnect + REST backfill for missed 1m bars |
| WS disconnect (> 5 min) | Heartbeat + timestamp gap | Full REST backfill from last known bar to current |
| Exchange maintenance | Bybit announces via API | Gracefully pause all strategies, resume on reconnect |
| VPN dropout | Connection failure across all WS | Auto-reconnect with exponential backoff; no trades during gap |
| Corrupt candle (OHLCV validation) | O > H or L > C | Discard bar, request via REST, log warning |

---

## 8. AI Agent Evaluation

### 8.1 Current Agent Inventory (27 agents)

The current system has 27 agents, of which 12 feed the OrchestratorEngine's meta-signal. The remaining 15 are support agents (position_monitor, scalp, twitter, reddit, telegram, narrative, etc.). Each agent runs in its own QThread, polling external APIs on intervals from 30s to 6 hours.

### 8.2 Evaluation Framework

Each agent is evaluated on three criteria:

1. **Signal relevance to intraday trading:** Does the signal contain information actionable on a 10–90 minute time horizon?
2. **Latency impact:** Does the agent's poll interval and processing time fit within the intraday pipeline?
3. **Signal quality:** Is the signal demonstrably correlated with short-term price movement, or is it noise?

### 8.3 Agent Disposition

#### Retained (4 agents)

**1. `funding_rate_agent` — RETAIN as bias modifier**

Rationale: Perpetual funding rates are directly relevant to intraday crypto trading. Extreme funding (> ±0.05%) predicts short-term mean reversion (shorts get squeezed, longs get flushed). Current poll interval (300s) is acceptable — funding rates change slowly. The signal is incorporated as a ±0.10 modifier to Stage A bias_score, not as a standalone trade signal.

Integration: On poll, publish to `AgentPool`. Stage A reads cached value when computing bias_score. If funding > +0.05%, reduce long bias_score by 0.10, increase short bias_score by 0.10 (contrarian). Vice versa for negative funding.

**2. `liquidation_flow_agent` — RETAIN as risk modifier**

Rationale: Liquidation cascades are the primary tail risk in crypto intraday trading. Large liquidation events (> $10M in 5 minutes) cause sharp, rapid price movements that invalidate technical setups. Current poll interval (60s) is appropriate. The signal gates trade entry: if liquidation volume in the last 5 minutes exceeds threshold, all new entries are suppressed until volume subsides (2-minute cooldown after last spike).

Integration: Continuous monitoring. Publishes `LIQUIDATION_ALERT` if threshold exceeded. Stage B checks alert status before submitting orders.

**3. `crash_detection_agent` — RETAIN (already feeds CrashDefenseController)**

Rationale: The 7-component crash detection system is critical for portfolio protection. It operates on a 60s poll interval and its tiered response (DEFENSIVE → SYSTEMIC) is essential for any trading strategy. No changes needed — it already functions independently of strategy design.

Integration: Unchanged. CrashDefenseController continues to scale position sizes and auto-execute defensive actions per existing tier system.

**4. `order_book_agent` — RETAIN as spread/liquidity monitor**

Rationale: L2 order book data is directly relevant for intraday execution. Bid-ask spread and book depth determine whether entries are feasible without excessive slippage. Current poll interval (30s) is appropriate. Repurposed from directional signal to execution feasibility check.

Integration: On poll, compute bid-ask spread and book depth for each watched symbol. Stage B checks: if spread > 0.15% or depth within 0.5% of price < $50K, flag asset as "thin" and either widen stops or skip entry.

#### Removed (23 agents)

| Agent | Reason for Removal |
|---|---|
| `macro_agent` (3600s) | F&G index, DXY, US yields change over hours/days. No actionable intraday signal. Macro regime is captured by 1h bias layer. |
| `options_flow_agent` (900s) | Crypto options flow is sparse and lagged (15-min poll). Institutional options positioning is relevant for days, not minutes. |
| `social_sentiment_agent` (1800s) | 30-minute poll aggregating Twitter/Reddit. By the time sentiment arrives, the intraday move is over. |
| `news_agent` (900s) | FinBERT NLP on RSS feeds. News impact on crypto is immediate (< 1 minute) or irrelevant. A 15-minute poll misses the move entirely. |
| `geopolitical_agent` (21600s) | 6-hour poll. Regulatory/geopolitical events affect multi-day trends, not intraday. |
| `sector_rotation_agent` (14400s) | 4-hour poll tracking TLT/XLU momentum. Zero intraday relevance. |
| `onchain_agent` (3600s) | Whale transfers and exchange flows are daily/weekly signals. |
| `volatility_surface_agent` (900s) | IV skew and term structure move slowly. Relevant for options, not spot intraday. |
| `coinglass_agent` | Redundant with funding_rate_agent. |
| `whale_agent` | Large transfers are daily signals. |
| `stablecoin_agent` | Supply ratio changes over weeks. |
| `miner_flow_agent` | Miner outflows are weekly signals. |
| `liquidity_vacuum_agent` | Concept absorbed into LSR strategy's own level detection. |
| `squeeze_detection_agent` | Concept absorbed into MX strategy's BB compression check. |
| `position_monitor_agent` | Replaced by ExecutionManager's internal position tracking. |
| `scalp_agent` | Replaced by the 5 new intraday strategies. |
| `twitter_agent` | Social signal too slow (1800s poll) for intraday. |
| `reddit_agent` | Same as twitter. |
| `telegram_agent` | Same. |
| `narrative_agent` | Market narratives evolve over days. |
| `liquidation_intelligence_agent` | Merged into liquidation_flow_agent. |

**Thread reduction:** Current baseline is ~51 threads (23 agent QThreads + core services). Removing 23 agents and converting to `threading.Thread` reduces to ~12 threads (4 agents + WS loop + main + REST worker pool). This eliminates context-switching overhead and simplifies debugging.

### 8.4 OrchestratorEngine Disposition

**REMOVE from execution path.** The OrchestratorEngine's weighted meta-signal aggregation across 12 agents is replaced by direct integration of the 4 retained agents into the Stage A bias_score. Rationale:

1. With only 4 agents, a weighted aggregator adds complexity without benefit — each agent's contribution is a simple modifier.
2. The meta-signal was designed for swing trading where slow-arriving intelligence improves multi-hour position quality. For intraday, speed of execution matters more than depth of consensus.
3. The staleness decay, veto logic, and regime-conditional weighting in OrchestratorEngine are unnecessary when agents are used as bias modifiers rather than signal generators.

The 4 retained agents publish directly to the EventBus. Strategy Stage A reads their cached values as context — no aggregation layer needed.

---

## 9. Execution Model

### 9.1 Order Execution Strategy

**Primary:** Limit orders at the next candle's open price ± spread/2. On 1m timeframes, the spread between a limit order at the ask (for longs) and the next bar's open is typically < 0.02% on liquid pairs. If the limit order is not filled within 5 seconds, convert to market order (IOC — immediate or cancel).

**Rationale for limit-first:** At 0.04%/side fees on Bybit, maker orders cost 0.02%/side (50% fee reduction). Over 20 trades/day, this saves 0.04% × 20 = 0.8% daily. Over 250 trading days, this is 200% annual fee savings. However, the 5-second fill window prevents the system from missing entries entirely.

**Slippage model:**
- Liquid pairs (BTC, ETH, SOL, BNB): Expected slippage 0.01–0.03% per fill.
- Mid-cap pairs (AVAX, LINK, DOT, etc.): Expected slippage 0.03–0.08%.
- Low-cap pairs: Expected slippage 0.05–0.15%. Pairs with typical spread > 0.15% are excluded from the universe (see Section 11).

**Spread awareness:** Before order submission, check current bid-ask spread from the `order_book_agent` cache. If spread > 0.10%, widen the stop loss by spread/2 to avoid being stopped out by the spread. If spread > 0.20%, defer the entry by 1 bar (60 seconds) and re-evaluate.

### 9.2 Timing Constraints

| Metric | Budget | Enforcement |
|---|---|---|
| Signal → order submission | < 500ms | Hard timeout; if exceeded, log and skip entry |
| Order submission → fill confirmation | < 5s (limit) or < 1s (market) | Convert to market after 5s unfilled limit |
| Partial exit signal → order | < 500ms | Same as entry |
| Emergency exit (crash defense) → order | < 200ms | Immediate market order, no limit attempt |

### 9.3 Fail-Safe Conditions

1. **Connection loss during open positions:** If WS disconnects and REST healthcheck fails for > 30 seconds, submit market close orders for all open positions via REST. Rationale: cannot manage stops without price data.
2. **Order rejection by exchange:** Log rejection reason, mark trade as "rejected_by_exchange", do not retry. Common reasons: insufficient margin, price moved beyond limit, symbol halted.
3. **Duplicate order prevention:** Each `SetupCandidate` has a unique ID. Once a trigger fires and an order is submitted, the setup is consumed (removed from ActiveSetups). If the order fails, the setup is not reinstated (it may fire again on the next 5m close if conditions persist).
4. **Position state desync:** On every 1m tick, reconcile local position state with exchange position via REST query (lightweight, 1 call per symbol with open positions). If discrepancy detected, exchange state is authoritative.

### 9.4 Headless Execution Architecture

```
ExecutionManager (pure Python, no Qt)
├── OrderRouter
│   ├── submit_limit(symbol, side, qty, price, timeout_ms=5000)
│   ├── submit_market(symbol, side, qty)
│   ├── cancel(order_id)
│   └── reconcile(symbol) — compare local vs exchange state
│
├── PositionTracker
│   ├── on_fill(order_id, fill_price, fill_qty, fee)
│   ├── on_tick(symbol, price) — evaluate SL/TP/trailing/time_stop
│   ├── open_positions: dict[symbol, list[Position]]
│   └── Persistence: JSON snapshot + SQLite trade records (unchanged)
│
├── PnL Engine
│   ├── Daily P&L tracking (UTC day boundary)
│   ├── Rolling 50-trade statistics (WR, PF, expectancy)
│   ├── Real-time drawdown calculation
│   └── Publishes PERFORMANCE_UPDATE on every trade close
│
└── SafetyGuards
    ├── daily_loss_limit: -2% of starting capital
    ├── drawdown_breaker: -10% from peak
    ├── max_concurrent: 8 positions (increased from 5 for intraday)
    ├── per_symbol_max: 2 positions (reduced from 10 for intraday)
    └── trade_rate_limit: max 5 entries per 5-minute window (anti-runaway)
```

---

## 10. Risk & Capital Model

### 10.1 Position Sizing

**Method:** Risk-based sizing (unchanged formula, new parameters).

```
risk_usdt = (risk_pct / 100) × available_capital
quantity = risk_usdt / stop_distance_usdt
position_size = quantity × entry_price
cap: position_size ≤ max_capital_pct × available_capital
```

**Parameter changes for intraday:**

| Parameter | Current (Swing) | Target (Intraday) | Rationale |
|---|---|---|---|
| `risk_pct_per_trade` | 0.50% | 0.25% | Half the risk per trade because trade count is 10× higher. Daily risk budget preserved: 0.25% × 20 trades = 5% max theoretical daily risk. |
| `max_capital_pct` | 4% | 3% | Slightly tighter per-trade cap. With max 8 concurrent positions × 3% = 24% max deployment. |
| `max_concurrent_positions` | 5 | 8 | More concurrent positions needed for 15–30 trades/day with 10–90min holds. At any given time, ~4–6 positions are typically open. |
| `max_positions_per_symbol` | 10 | 2 | Only 2 positions per symbol to prevent concentration. Different strategies may have one position each on the same symbol. |

### 10.2 Portfolio Heat Management

**Current:** Heat = Σ(position_size × stop_distance_pct), reject if heat > 6%.

**Intraday adaptation:** Heat limit increased to 8% because stop distances are tighter (0.8–1.5× ATR on 5m vs 2.5× ATR on 30m). With tighter stops, total portfolio risk per position is smaller, allowing more concurrent exposure.

Heat calculation remains identical — it naturally adapts to tighter stops.

### 10.3 Daily Loss Limit

**Hard stop:** If realized + unrealized P&L for the UTC day drops below -2% of starting equity, all positions are closed and no new trades are entered until the next UTC day.

**Soft warning:** At -1.5%, reduce `risk_pct_per_trade` to 0.15% (half-size mode) and disable the two most aggressive strategies (MX and LSR). This provides a graceful degradation before the hard stop.

### 10.4 Drawdown Circuit Breakers

| Drawdown Level | Action |
|---|---|
| -5% from peak | Reduce `max_concurrent_positions` from 8 to 5. Log alert. |
| -8% from peak | Reduce `risk_pct_per_trade` to 0.15%. Disable MX and LSR. |
| -10% from peak | Close all positions. No trading for 24 hours. Full system review required. |
| -15% from peak | Close all positions. System shutdown. Manual restart required. |

These are cumulative — -8% triggers both the -5% and -8% actions.

### 10.5 Crash Defense Integration

The existing CrashDefenseController tier system (NORMAL → DEFENSIVE → HIGH_ALERT → EMERGENCY → SYSTEMIC) is retained unchanged. Its position-size multipliers (1.0 → 0.65 → 0.35 → 0.10 → 0.0) apply on top of the intraday risk parameters. During a DEFENSIVE tier event, intraday risk becomes 0.25% × 0.65 = 0.16% per trade.

### 10.6 Capital Rotation Model

Target capital utilization: 60–80% during active hours, < 20% during low-volatility periods.

**Mechanism:** The time stop (45–90 minutes per strategy) ensures capital is freed regularly. If a position is consuming capital without approaching TP or SL, it is exited at the time stop, freeing capital for new setups.

**Anti-idle rule:** If no new setups have been generated for > 60 minutes across all assets, reduce the Stage A `bias_score` threshold from 0.35 to 0.30 (wider net for setups, but trigger requirements remain unchanged). This prevents the system from becoming overly selective during quieter periods.

---

## 11. Asset Universe Strategy

### 11.1 Universe Selection Criteria

For intraday trading, liquidity and spread dominate all other factors. An asset with a 0.15% spread requires 0.30% of movement just to break even after entry and exit — consuming 30–50% of typical intraday R for many setups.

**Selection criteria (all must be met):**

1. **24h volume > $50M** on Bybit perpetual. Below this threshold, order book depth is insufficient for reliable fills at target position sizes ($500–$2000 per trade at Phase 1 risk levels).
2. **Typical bid-ask spread < 0.08%** during active hours (UTC 08:00–20:00). Measured as median spread over 7 days.
3. **1m candle availability** via Bybit WS API. Some smaller pairs may have spotty 1m data.
4. **ATR/close ratio > 0.3%** on 1h timeframe. Below this, the asset doesn't move enough for intraday trades to overcome fees.
5. **No exchange-specific risk flags** (delisting announced, funding mechanism changes, etc.).

### 11.2 Recommended Universe (16 assets)

Based on Bybit perpetual liquidity as of April 2026:

**Tier 1 — Core (always active, highest allocation priority):**
BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT, XRP/USDT

**Tier 2 — Standard (active unless liquidity drops):**
DOGE/USDT, ADA/USDT, AVAX/USDT, LINK/USDT, DOT/USDT, MATIC/USDT

**Tier 3 — Rotational (active when volatility is sufficient):**
NEAR/USDT, ARB/USDT, OP/USDT, SUI/USDT, APT/USDT

**Not recommended:** Going beyond 16 assets. Rationale: with 5 strategies × 16 assets = 80 potential setups to monitor every 5 minutes. This is computationally manageable and provides sufficient diversification. Adding 4 more assets from Tier 4 (lower liquidity) would increase spread costs and execution risk without proportional benefit.

**Dynamic adjustment:** The `order_book_agent` continuously monitors spread and depth. If a Tier 2/3 asset's spread exceeds 0.15% for > 1 hour, it is temporarily removed from the active universe. A Tier 4 asset with strong volatility and tight spreads can be promoted.

### 11.3 Per-Asset Allocation

No fixed per-asset allocation. The `SymbolAllocator` adjusts `bias_score` based on recent per-asset performance (L2 asset adjustment, ±8%). Beyond this, all assets compete equally for capital through the RiskGate's standard position limiting.

**Correlation guard:** The existing `CorrelationController` prevents excessive exposure to correlated moves. For crypto specifically: BTC and ETH move together (~0.85 correlation). If the system has long positions in both BTC and ETH, the combined exposure counts as 1.85× of a single position for heat calculation purposes.

---

## 12. Performance Validation Plan

### 12.1 Backtesting Methodology

#### 12.1.1 Data Requirements

| Item | Specification |
|---|---|
| Source | Bybit historical API (kline endpoint) |
| Resolution | 1m OHLCV |
| Coverage | All 16 universe assets |
| Duration | Minimum 18 months (2024-10-01 to 2026-04-01) |
| Derived timeframes | 3m, 5m, 15m, 1h constructed from 1m |
| Storage | Local SQLite database, ~2GB estimated for 16 assets × 18mo × 1m bars |

**Data acquisition plan:**

1. **Bybit API limits:** The kline endpoint returns up to 1000 bars per request. For 1m data over 18 months: ~777,600 bars per asset. This requires ~778 requests per asset, 12,448 total. At 10 requests/second (conservative), acquisition takes ~21 minutes.
2. **Validation:** After download, validate: (a) no gaps > 3 minutes, (b) OHLCV consistency (O between prev H and L, H ≥ max(O,C), L ≤ min(O,C)), (c) volume > 0 on all bars, (d) timestamp monotonically increasing.
3. **Gap handling:** If gaps exist, attempt fill from alternative source (Binance 1m klines for cross-reference). If unfillable, mark the gap and exclude any strategy evaluation that spans it.
4. **Storage schema:** `bars(symbol TEXT, timestamp INTEGER, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY(symbol, timestamp))`. Index on `(symbol, timestamp)` for range queries.
5. **Refresh:** Weekly refresh appends new 1m bars. Full re-download quarterly to catch any exchange corrections.

#### 12.1.2 Execution Simulation Rules

1. **No same-bar fills.** Signal generated on bar N close → entry at bar N+1 open. This adds 60 seconds of realistic delay.
2. **Slippage model:** Entry at `open ± slippage` where slippage = max(0.01%, half of typical spread for the asset). Spread data derived from historical order book snapshots if available, or estimated at 0.03% for Tier 1, 0.06% for Tier 2, 0.10% for Tier 3.
3. **Fee model:** 0.04% per side (taker) or 0.02% per side (maker). Conservative baseline uses taker fees for all trades. Sensitivity analysis includes maker fee scenario.
4. **No look-ahead bias.** All indicators use only data available at bar close time. Regime classification uses only bars up to and including the current bar.
5. **Warm-up exclusion.** First 200 1m bars (3.3 hours) of each backtest session are excluded from trade counting (indicator warm-up period).
6. **AI agents excluded.** Backtests use `technical_only=True` — no agent data in bias_score. Agent contribution is evaluated separately in forward testing.

#### 12.1.3 Backtest Scenarios

| Scenario | Fees | Slippage | Agent Bias | Purpose |
|---|---|---|---|---|
| A (baseline) | 0.04%/side | 0.03% | None | Conservative baseline |
| B (maker) | 0.02%/side | 0.02% | None | Optimistic with limit orders |
| C (stress) | 0.06%/side | 0.08% | None | High-slippage stress test |
| D (agent bias) | 0.04%/side | 0.03% | Simulated | Forward-looking agent impact test |

### 12.2 Target Metrics

| Metric | Minimum Target | Stretch Target | Failure Threshold |
|---|---|---|---|
| Profit Factor (PF) | > 1.30 (Scenario A) | > 1.50 | < 1.15 |
| Win Rate | > 48% | > 55% | < 42% |
| Expected R per trade | > +0.15R | > +0.25R | < +0.05R |
| CAGR (scaled to target capital) | > 30% | > 50% | < 15% |
| Max Drawdown | < 15% | < 10% | > 20% |
| Trades per day (avg) | > 12 | 15–30 | < 8 |
| Sharpe Ratio (annualized) | > 1.5 | > 2.5 | < 1.0 |
| Avg hold time | 10–90 minutes | 20–60 minutes | > 120 minutes |

**Per-strategy minimums:** Each strategy must independently achieve PF > 1.10 (Scenario A). Any strategy below 1.10 is disabled before production. The combined portfolio target is PF > 1.30 — individual strategies may be below this if the portfolio diversification benefit lifts the aggregate.

### 12.3 Walk-Forward Validation

To guard against overfitting, all parameter optimization uses walk-forward analysis:

1. **In-sample window:** 12 months
2. **Out-of-sample window:** 3 months
3. **Step:** 3 months forward
4. **Periods:** 3 walk-forward periods covering 18 months total

Parameters optimized in-sample: EMA periods, ATR multipliers for stops/targets, volume threshold multipliers, RSI thresholds. Parameters NOT optimized (fixed across all periods): strategy logic, timeframe assignments, candle pattern definitions.

A strategy passes walk-forward validation if: the out-of-sample PF is within 30% of the in-sample PF across all 3 periods, AND the out-of-sample PF exceeds 1.10 in at least 2 of 3 periods.

### 12.4 Forward Testing (Paper Trading)

**Duration:** 4 weeks minimum before any live capital deployment.

**Process:**
1. Week 1–2: Run headless core on paper trading with all 5 strategies. Monitor: trade frequency, P&L trajectory, per-strategy metrics, execution latency (signal → order), data pipeline reliability (WS uptime, gap recovery).
2. Week 3: Disable any strategy with paper trading PF < 1.05 or unreasonable trade characteristics (e.g., clustering of entries, excessive stop-outs within 2 bars).
3. Week 4: Stable operation with remaining strategies. Verify daily trade count matches backtest expectations (within ±50%).

**Paper-to-live checklist:**
- 4 consecutive weeks of paper trading completed
- Pipeline uptime > 99.5% (total WS + REST)
- No crash defense activations caused by system bugs (only market-driven)
- Trade count within 50% of backtest expectation
- PF > 1.0 on paper (not required to hit 1.30 target — 4 weeks is too short for statistical significance on PF, but must not be net negative)

### 12.5 Latency Measurement

Instrument every stage of the pipeline with microsecond timestamps:

| Measurement Point | Expected | Alert Threshold |
|---|---|---|
| WS message → CandleBuilder emit | < 5ms | > 50ms |
| CandleBuilder emit → Stage A complete (per asset) | < 100ms | > 500ms |
| CandleBuilder emit → Stage B trigger check (per asset) | < 20ms | > 100ms |
| Trigger fire → RiskGate complete | < 10ms | > 50ms |
| RiskGate approve → order submitted | < 50ms | > 200ms |
| Order submitted → fill confirmed | < 5000ms (limit) | > 10000ms |
| **End-to-end: candle close → fill** | **< 6000ms** | **> 15000ms** |

Log all measurements to a time-series file for post-session analysis. Weekly latency reports comparing P50, P95, P99 across all pipeline stages.

---

## 13. Migration Plan

### 13.1 Phase Overview

| Phase | Duration | Deliverable | Risk Level |
|---|---|---|---|
| Phase 0: Data Infrastructure | 2 weeks | Historical data pipeline, 1m storage, validation | Low |
| Phase 1: Headless Core | 3 weeks | Pure-Python EventBus, headless execution loop | Medium |
| Phase 2: WebSocket Data Engine | 2 weeks | WS ingestion, CandleBuilder, REST fallback | Medium |
| Phase 3: Strategy Implementation | 4 weeks | 5 intraday strategies, Stage A/B pipeline | High |
| Phase 4: Risk Adaptation | 1 week | Updated risk parameters, new limits | Low |
| Phase 5: Integration & Paper Test | 4 weeks | Full system paper trading, latency validation | Medium |
| Phase 6: Web Dashboard | 2 weeks (parallel) | Monitoring interface (can start after Phase 1) | Low |

**Total estimated duration:** 12–14 weeks (Phases 5 and 6 overlap)

### 13.2 Phase Details

#### Phase 0: Data Infrastructure (Weeks 1–2)

**Deliverables:**
1. `DataAcquisition` module: Bybit historical 1m kline downloader with rate limiting, gap detection, validation
2. SQLite storage for 1m bars (all 16 universe assets, 18 months)
3. Candle derivation utility (1m → 3m, 5m, 15m, 1h) with validation tests
4. Data quality report for all assets

**Dependencies:** None (standalone)
**Risk:** Bybit rate limits may slow acquisition. Mitigation: parallelize across 3 API keys (demo accounts).

#### Phase 1: Headless Core (Weeks 3–5)

**Deliverables:**
1. `EventBusPure`: Pure-Python replacement for Qt-based `EventBus`. Same topic structure, same API (`subscribe`, `publish`), but using `threading.Lock` + Python callbacks instead of Qt signals. Tests: 100% coverage of current EventBus test suite passing on new implementation.
2. `ExecutionManager`: Refactor `PaperExecutor` to remove all Qt dependencies. `on_tick()` called from the data loop thread directly (no Qt signal marshaling). Position persistence unchanged (JSON + SQLite).
3. Headless main loop: `main_headless.py` that initializes ExchangeManager, EventBusPure, ExecutionManager, RiskGate, CrashDefenseController without any Qt imports. Verify: `python main_headless.py` runs and processes simulated ticks.

**Dependencies:** None
**Risk:** Subtle Qt coupling in existing code (e.g., `QTimer.singleShot` calls buried in modules). Mitigation: grep for all Qt imports; create adapter layer where needed.

#### Phase 2: WebSocket Data Engine (Weeks 6–7)

**Deliverables:**
1. `WSManager`: ccxt.pro WebSocket client running in dedicated `asyncio` thread. Subscribe to 1m klines for all universe symbols. Automatic reconnection with exponential backoff. Heartbeat monitoring.
2. `CandleBuilder`: In-memory ring buffers per symbol × timeframe. Candle aggregation (1m → higher TFs). Gap detection and REST backfill trigger.
3. Integration test: WSManager → CandleBuilder → emit `CANDLE_CLOSE` events → verify all timeframes update correctly over 24-hour test run.

**Dependencies:** Phase 1 (EventBusPure for event emission)
**Risk:** WS stability on residential internet + VPN. Mitigation: extensive reconnection testing; 24-hour soak test before Phase 3.

#### Phase 3: Strategy Implementation (Weeks 8–11)

**Deliverables:**
1. `StrategyBus`: Orchestrator for Stage A (setup) and Stage B (trigger). Manages ActiveSetups registry.
2. Five strategy implementations (MX, VR, MPC, RBR, LSR), each as a subclass of `BaseIntradayStrategy` with `evaluate_setup(symbol, data_5m, data_15m, data_1h)` and `evaluate_trigger(symbol, data_1m, data_3m, setup)` methods.
3. `RegimeEngine`: Adapted HMM classifier for 15m resolution with 4 states.
4. Backtesting engine adapted for 1m resolution with the new strategies.
5. Backtest results for all 4 scenarios (A, B, C, D) across all 16 assets.
6. Walk-forward validation results.

**Dependencies:** Phase 0 (historical data), Phase 2 (CandleBuilder for live testing)
**Risk:** Highest-risk phase. Strategy performance may not meet targets. Mitigation: develop and test strategies incrementally; if a strategy fails walk-forward validation, remove it rather than force-fitting.

#### Phase 4: Risk Adaptation (Week 12)

**Deliverables:**
1. Updated `config.yaml` risk parameters (per Section 10)
2. Daily loss limit and drawdown circuit breaker implementation in ExecutionManager
3. Trade rate limiter (max 5 entries per 5-minute window)
4. Updated position sizing with 0.25% risk per trade

**Dependencies:** Phase 3 (strategy implementation)
**Risk:** Low (parameter changes + simple new controls)

#### Phase 5: Integration & Paper Test (Weeks 12–15)

**Deliverables:**
1. Full system integration: WSManager → CandleBuilder → StrategyBus → RiskGate → ExecutionManager → PositionTracker
2. 4-week paper trading run with full instrumentation
3. Latency measurements across all pipeline stages
4. Weekly performance reports
5. Paper-to-live readiness assessment

**Dependencies:** All previous phases
**Risk:** Medium. Integration issues between components. Mitigation: integration tests from Phase 2 onward.

#### Phase 6: Web Dashboard (Weeks 10–13, parallel)

**Deliverables:**
1. WebSocket API server (localhost:8765) exposing: positions, P&L, trade history, active setups, pipeline status, latency metrics
2. Lightweight web dashboard (React or plain HTML) connecting to the WS API
3. Qt GUI adapter: if Qt GUI is retained for monitoring, a thin adapter that connects to the WS API instead of directly subscribing to EventBus

**Dependencies:** Phase 1 (headless core)
**Risk:** Low (non-critical path, can lag behind core development)

### 13.3 Rollback Strategy

Each phase has a clean rollback path:

- **Phase 0:** Data infrastructure is additive; rollback = ignore new module.
- **Phase 1–2:** Headless core runs alongside existing Qt system. If headless fails, revert to Qt-only.
- **Phase 3:** New strategies are added as new files, not modifications to existing strategy code. Rollback = disable new strategies in config, re-enable PBL/SLC.
- **Phase 4:** Risk parameters stored in config.yaml. Rollback = restore previous config values.
- **Phase 5–6:** Paper trading and dashboard are non-destructive.

At no point during migration is the existing system's ability to operate in its current (swing) mode compromised. The migration is entirely additive until Phase 5, where the new system is validated on paper before any commitment.

---

## 14. Risks & Tradeoffs

### 14.1 Technical Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **WS instability on residential internet** | Medium | High (data gaps → missed trades or wrong SL) | REST fallback with 5s poll interval during WS outage. Aggressive reconnection. 24h soak test before deployment. |
| **1m data noise causing false triggers** | Medium | Medium (overtrading, reduced PF) | Two-stage pipeline ensures 5m setup qualification before any 1m trigger is evaluated. Stage A acts as noise filter. |
| **Singapore VPN adds latency variability** | High | Low-Medium (50–150ms jitter) | Latency budget accounts for worst-case 150ms. Limit-first execution absorbs jitter. Monitor VPN and switch providers if P99 > 200ms. |
| **Strategy overfitting to backtest data** | Medium | High (negative P&L in production) | Walk-forward validation. 4-week paper trading. Per-strategy PF minimum of 1.10 out-of-sample. |
| **HMM regime classifier instability at 15m** | Low-Medium | Medium (regime flipping causes setup invalidation) | 4-hour HMM refit interval (not per-bar). Rule-based fallback when HMM confidence < 50%. |
| **Thread count reduction causes subtle bugs** | Low | Medium | Extensive unit + integration tests. 24h soak test. EventBus regression tests on pure-Python implementation. |

### 14.2 Strategic Tradeoffs

**Tradeoff 1: Speed vs. Signal Quality.**
The redesign explicitly favors speed (1m trigger, WS data, < 500ms signal-to-order) over the current system's deep signal quality (multi-model confluence, 12-agent meta-signal, MTF confirmation across 3+ timeframes). The mitigation is the two-stage pipeline: Stage A provides quality filtering at 5m resolution, and Stage B provides speed at 1m resolution. The risk is that the reduced confluence depth allows more noise-driven trades. This is monitored via per-strategy WR in paper testing — if WR drops below 42%, the strategy is disabled.

**Tradeoff 2: Agent Signal Depth vs. Latency.**
Reducing from 12 to 4 agents removes potential edge from macro, sentiment, and options flow intelligence. The justification is empirical: the current system with all 12 agents active produces net-zero agent contribution to intraday signals (Session 51 documented that all agents returned signal=0.0 in neutral conditions, and the orchestrator meta-signal range was [-0.115, +0.064] — too narrow to meaningfully affect trade decisions). The retained 4 agents (funding, liquidation, crash detection, order book) address concrete, high-impact intraday factors rather than diffuse intelligence.

**Tradeoff 3: Universe Size (16 vs. 20).**
The brief specified ~20 assets. This design recommends 16 with dynamic rotation from a Tier 3 pool. The tradeoff: fewer simultaneous opportunities vs. better execution quality (tighter spreads, deeper books, fewer slippage-related losses). At 15–30 trades/day, 16 assets provide more than sufficient opportunity. Adding 4 marginal assets would contribute ~2 trades/day with worse fills.

**Tradeoff 4: Holding Period Constraint (90-minute max).**
The time stop forces exit of positions that haven't hit SL or TP within 45–90 minutes (strategy-dependent). This sacrifices potential winners that need more time to develop. The counter-argument: intraday setups that haven't materialized within their expected holding period have likely been invalidated by subsequent price action. The time stop prevents the system from drifting into unintentional swing trading. Backtest validation will measure the percentage of time-stopped trades that would have been profitable with longer holds — if > 30%, the time stop should be extended.

**Tradeoff 5: Headless Core Development Cost.**
Decoupling the execution engine from Qt requires significant refactoring (Phase 1, 3 weeks). The alternative was to keep Qt and simply speed up the scan cadence. However, the Qt thread constraints (main-thread-only `on_tick()`, QueuedConnection overhead, shared event loop with 20 GUI pages) create a structural ceiling on execution speed. The headless approach eliminates this ceiling permanently and enables future deployment to cloud VMs without Qt dependencies.

### 14.3 Dependencies and Assumptions

1. **Bybit Demo API remains available** at `api-demo.bybit.com` with the same WebSocket kline subscription API as production. If Bybit changes their demo API, WS testing will need to use production (read-only).
2. **Singapore VPN remains functional** for Bybit access. If Singapore is blocked, the system needs VPN failover to another acceptable region (Tokyo was previously used but caused 403s — may need to test Korea or Hong Kong).
3. **ccxt.pro WebSocket support for Bybit** continues to be maintained. The ccxt library is under active development; breaking changes to the WS API require version pinning and testing on upgrades.
4. **Crypto market microstructure** (perpetual futures, funding rates, liquidation mechanics) remains consistent. A major exchange-level change (e.g., Bybit removing perpetual funding or changing fee structure) would require strategy parameter recalibration.
5. **RTX 4070 GPU** is not used for the intraday pipeline itself (no FinBERT inference in the critical path). GPU remains available for offline model training and backtesting acceleration.

---

## Appendix A: Indicator Computation Matrix

| Indicator | 1m | 3m | 5m | 15m | 1h | Used By |
|---|---|---|---|---|---|---|
| EMA(9) | | | ✓ | ✓ | | MPC |
| EMA(20) | | | ✓ | ✓ | ✓ | MX, MPC, Regime |
| EMA(21) | | | ✓ | ✓ | | MPC |
| EMA(50) | | | ✓ | ✓ | ✓ | MPC, MX, RBR bias |
| ADX(14) | | | | ✓ | ✓ | MPC, MX, Regime |
| ATR(14) | | | ✓ | | | All (stop/target sizing) |
| RSI(14) | ✓ | | ✓ | | | MX, VR, MPC, LSR |
| Bollinger(20,2) | | | ✓ | | | MX (compression) |
| Volume MA(20) | ✓ | ✓ | ✓ | | | All (volume confirm) |
| VWAP (session) | ✓ | | ✓ | | | VR |
| Range(N-bar H/L) | | | ✓ | | | MX, RBR |
| Swing H/L (10) | | | ✓ | | | LSR (liquidity levels) |

## Appendix B: Configuration Parameter Reference

```yaml
# Target config.yaml additions for intraday redesign
intraday:
  enabled: true
  scan_mode: "ws_driven"              # ws_driven | poll_5m (fallback)

timeframes:
  trigger: ["1m", "3m"]
  setup: ["5m", "15m"]
  bias: ["15m", "1h"]

strategies:
  momentum_expansion:
    enabled: true
    consolidation_min_bars: 6          # 5m bars
    consolidation_max_width_atr: 1.5
    bb_percentile_threshold: 30
    volume_breakout_mult: 1.8
    rsi_long_min: 55
    rsi_short_max: 45
    stop_atr_mult: 1.2
    target_r_mult: 2.5
    partial_pct: 0.50
    partial_r_trigger: 1.5
    time_stop_minutes: 90

  vwap_reclaim:
    enabled: true
    deviation_min_atr: 0.5
    approach_tolerance_atr: 0.15
    volume_reclaim_mult: 1.3
    rejection_wick_body_ratio: 2.0
    stop_atr_mult: 0.8
    target_r_reclaim: 2.0
    target_r_rejection: 1.5
    time_stop_minutes: 60
    vwap_cross_invalidation_atr: 0.3

  micro_pullback:
    enabled: true
    adx_min: 25
    pullback_ema_tolerance_atr: 0.3
    pullback_min_bars: 2
    pullback_max_bars: 5
    rsi_pullback_floor: 40
    rsi_recent_strength_min: 60
    reversal_volume_mult: 1.2
    target_r_mult: 2.0
    time_stop_minutes: 45

  range_breakout_reclaim:
    enabled: true
    range_min_bars: 12
    range_width_min_atr: 0.8
    range_width_max_atr: 2.5
    retest_max_bars: 6
    retest_tolerance_atr: 0.3
    volume_retest_mult: 1.5
    stop_inside_range_pct: 0.5        # stop at 50% of range width inside
    target_range_mult: 1.5
    partial_pct: 0.50
    time_stop_minutes: 75

  liquidity_sweep:
    enabled: true
    sweep_min_penetration_atr: 0.3
    volume_spike_mult: 2.0
    stop_beyond_sweep_atr: 0.2
    target_ranging_r: 2.0
    target_trending_r: 1.5
    time_stop_minutes: 60
    second_sweep_bars: 5               # exit if price returns within 5 bars

risk_engine:
  risk_pct_per_trade: 0.25
  max_capital_pct: 0.03
  max_concurrent_positions: 8
  max_positions_per_symbol: 2
  portfolio_heat_max_pct: 0.08
  daily_loss_limit_pct: -0.02
  daily_loss_soft_pct: -0.015
  trade_rate_limit: 5                  # max entries per 5-min window

drawdown_breakers:
  level_1_pct: 0.05                    # reduce max concurrent to 5
  level_2_pct: 0.08                    # half-size mode, disable MX+LSR
  level_3_pct: 0.10                    # close all, 24h pause
  level_4_pct: 0.15                    # shutdown

universe:
  tier_1: ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
  tier_2: ["DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT"]
  tier_3: ["NEARUSDT", "ARBUSDT", "OPUSDT", "SUIUSDT", "APTUSDT"]
  spread_max_pct: 0.15                 # auto-remove if exceeded for 1h
  volume_min_24h_usd: 50000000

agents:
  retained: ["funding_rate", "liquidation_flow", "crash_detection", "order_book"]
  disabled: ["macro", "options_flow", "social_sentiment", "news", "geopolitical",
             "sector_rotation", "onchain", "volatility_surface", "coinglass",
             "whale", "stablecoin", "miner_flow", "liquidity_vacuum",
             "squeeze_detection", "position_monitor", "scalp",
             "twitter", "reddit", "telegram", "narrative",
             "liquidation_intelligence"]
```

## Appendix C: Glossary

| Term | Definition |
|---|---|
| ATR | Average True Range — 14-period measure of bar-to-bar price volatility |
| Bias layer | Highest timeframe layer (15m/1h) establishing directional trend |
| CandleBuilder | Module that constructs higher-TF candles from 1m bars in real time |
| CAGR | Compound Annual Growth Rate |
| Heat | Sum of (position_size × stop_distance%) across all open positions — measure of total portfolio risk |
| LSR | Liquidity Sweep Reclaim strategy |
| MPC | Micro Pullback Continuation strategy |
| MX | Momentum Expansion strategy |
| PF | Profit Factor = gross_profit / gross_loss |
| R | Risk unit — 1R = the distance from entry to stop loss in dollar terms |
| RBR | Range Breakout Reclaim strategy |
| Setup layer | Mid timeframe layer (5m/15m) identifying structural trade setups |
| Stage A | Setup qualification — runs on 5m candle close |
| Stage B | Execution trigger — runs on 1m candle close |
| Trigger layer | Lowest timeframe layer (1m/3m) for precise entry timing |
| TTL | Time-to-live — expiration period for setup candidates |
| VR | VWAP Reclaim/Rejection strategy |
| VWAP | Volume-Weighted Average Price — session cumulative |
| Walk-forward | Backtesting methodology that tests on out-of-sample data to prevent overfitting |

---

*End of Document*
