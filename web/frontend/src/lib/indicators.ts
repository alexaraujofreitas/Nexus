/**
 * Technical indicator computation library for Chart Workspace.
 *
 * All functions take an array of OHLCV bars (sorted ascending by time)
 * and return arrays of {time, value} points compatible with
 * lightweight-charts LineSeries / HistogramSeries.
 */

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Point {
  time: number;
  value: number;
}

export interface MACDResult {
  macdLine: Point[];
  signalLine: Point[];
  histogram: Point[];
}

export interface BollingerResult {
  upper: Point[];
  middle: Point[];
  lower: Point[];
}

export interface SRLevel {
  price: number;
  type: 'support' | 'resistance';
  touches: number;
  firstSeen: number; // time
  lastSeen: number;  // time
}

// ── SIMPLE MOVING AVERAGE ─────────────────────────────────────

/**
 * SMA(n) = arithmetic mean of the last n closing prices.
 * Returns one point per bar starting from bar index n-1.
 */
export function calcSMA(bars: Bar[], period: number): Point[] {
  if (bars.length < period) return [];
  const result: Point[] = [];
  let sum = 0;

  for (let i = 0; i < bars.length; i++) {
    sum += bars[i].close;
    if (i >= period) {
      sum -= bars[i - period].close;
    }
    if (i >= period - 1) {
      result.push({ time: bars[i].time, value: sum / period });
    }
  }
  return result;
}

// ── EXPONENTIAL MOVING AVERAGE (internal helper) ──────────────

function ema(values: number[], period: number): number[] {
  if (values.length < period) return [];
  const k = 2 / (period + 1);
  const result: number[] = new Array(values.length).fill(NaN);

  // Seed with SMA of first `period` values
  let seed = 0;
  for (let i = 0; i < period; i++) seed += values[i];
  seed /= period;
  result[period - 1] = seed;

  for (let i = period; i < values.length; i++) {
    result[i] = values[i] * k + result[i - 1] * (1 - k);
  }
  return result;
}

// ── RSI (Wilder's smoothing) ──────────────────────────────────

/**
 * RSI = 100 - 100 / (1 + RS)
 * RS  = avg_gain / avg_loss  (Wilder-smoothed over `period` bars)
 */
export function calcRSI(bars: Bar[], period: number = 14): Point[] {
  if (bars.length < period + 1) return [];
  const result: Point[] = [];
  let avgGain = 0;
  let avgLoss = 0;

  // First `period` changes to seed averages
  for (let i = 1; i <= period; i++) {
    const change = bars[i].close - bars[i - 1].close;
    if (change > 0) avgGain += change;
    else avgLoss -= change;
  }
  avgGain /= period;
  avgLoss /= period;

  const rs0 = avgLoss === 0 ? 100 : avgGain / avgLoss;
  result.push({ time: bars[period].time, value: 100 - 100 / (1 + rs0) });

  // Wilder smoothing for remaining bars
  for (let i = period + 1; i < bars.length; i++) {
    const change = bars[i].close - bars[i - 1].close;
    const gain = change > 0 ? change : 0;
    const loss = change < 0 ? -change : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
    result.push({ time: bars[i].time, value: 100 - 100 / (1 + rs) });
  }
  return result;
}

// ── MACD ──────────────────────────────────────────────────────

/**
 * MACD Line    = EMA(12) - EMA(26)
 * Signal Line  = EMA(9) of MACD Line
 * Histogram    = MACD Line - Signal Line
 */
export function calcMACD(
  bars: Bar[],
  fastPeriod: number = 12,
  slowPeriod: number = 26,
  signalPeriod: number = 9,
): MACDResult {
  const closes = bars.map((b) => b.close);
  const fastEMA = ema(closes, fastPeriod);
  const slowEMA = ema(closes, slowPeriod);

  // MACD line starts where slowEMA is valid
  const macdValues: number[] = new Array(bars.length).fill(NaN);
  for (let i = slowPeriod - 1; i < bars.length; i++) {
    if (!isNaN(fastEMA[i]) && !isNaN(slowEMA[i])) {
      macdValues[i] = fastEMA[i] - slowEMA[i];
    }
  }

  // Collect valid MACD values for signal EMA computation
  const validMacd: number[] = [];
  const validIdx: number[] = [];
  for (let i = 0; i < macdValues.length; i++) {
    if (!isNaN(macdValues[i])) {
      validMacd.push(macdValues[i]);
      validIdx.push(i);
    }
  }

  const signalEMA = ema(validMacd, signalPeriod);

  const macdLine: Point[] = [];
  const signalLine: Point[] = [];
  const histogram: Point[] = [];

  for (let j = 0; j < validMacd.length; j++) {
    const barIdx = validIdx[j];
    const t = bars[barIdx].time;
    macdLine.push({ time: t, value: validMacd[j] });

    if (!isNaN(signalEMA[j])) {
      signalLine.push({ time: t, value: signalEMA[j] });
      histogram.push({ time: t, value: validMacd[j] - signalEMA[j] });
    }
  }

  return { macdLine, signalLine, histogram };
}

// ── BOLLINGER BANDS ───────────────────────────────────────────

/**
 * Middle = SMA(20)
 * Upper  = Middle + 2 * StdDev(20)
 * Lower  = Middle - 2 * StdDev(20)
 */
export function calcBollingerBands(
  bars: Bar[],
  period: number = 20,
  stdDevMult: number = 2,
): BollingerResult {
  if (bars.length < period) return { upper: [], middle: [], lower: [] };

  const upper: Point[] = [];
  const middle: Point[] = [];
  const lower: Point[] = [];

  for (let i = period - 1; i < bars.length; i++) {
    let sum = 0;
    for (let j = i - period + 1; j <= i; j++) sum += bars[j].close;
    const mean = sum / period;

    let sqSum = 0;
    for (let j = i - period + 1; j <= i; j++) {
      const d = bars[j].close - mean;
      sqSum += d * d;
    }
    const stdDev = Math.sqrt(sqSum / period);

    const t = bars[i].time;
    middle.push({ time: t, value: mean });
    upper.push({ time: t, value: mean + stdDevMult * stdDev });
    lower.push({ time: t, value: mean - stdDevMult * stdDev });
  }

  return { upper, middle, lower };
}

// ── SUPPORT / RESISTANCE ──────────────────────────────────────

/**
 * Identifies support and resistance levels using pivot high/low detection.
 *
 * Algorithm:
 * 1. Detect swing highs: bar where high is the highest in +-swingStrength bars
 * 2. Detect swing lows: bar where low is the lowest in +-swingStrength bars
 * 3. Cluster nearby levels within `tolerance` percentage
 * 4. Score by number of touches + recency
 * 5. Return top N levels
 */
export function calcSupportResistance(
  bars: Bar[],
  swingStrength: number = 5,
  tolerancePct: number = 0.5,
  maxLevels: number = 5,
): SRLevel[] {
  if (bars.length < swingStrength * 2 + 1) return [];

  const pivots: { price: number; type: 'support' | 'resistance'; time: number }[] = [];

  for (let i = swingStrength; i < bars.length - swingStrength; i++) {
    // Check swing high
    let isHigh = true;
    for (let j = i - swingStrength; j <= i + swingStrength; j++) {
      if (j !== i && bars[j].high >= bars[i].high) {
        isHigh = false;
        break;
      }
    }
    if (isHigh) {
      pivots.push({ price: bars[i].high, type: 'resistance', time: bars[i].time });
    }

    // Check swing low
    let isLow = true;
    for (let j = i - swingStrength; j <= i + swingStrength; j++) {
      if (j !== i && bars[j].low <= bars[i].low) {
        isLow = false;
        break;
      }
    }
    if (isLow) {
      pivots.push({ price: bars[i].low, type: 'support', time: bars[i].time });
    }
  }

  if (pivots.length === 0) return [];

  // Cluster nearby pivots
  const tolerance = tolerancePct / 100;
  const clusters: SRLevel[] = [];

  // Sort pivots by price
  const sorted = [...pivots].sort((a, b) => a.price - b.price);

  let cluster: typeof pivots = [sorted[0]];
  for (let i = 1; i < sorted.length; i++) {
    const prev = cluster[cluster.length - 1];
    if (Math.abs(sorted[i].price - prev.price) / prev.price <= tolerance) {
      cluster.push(sorted[i]);
    } else {
      // Flush cluster
      const avgPrice = cluster.reduce((s, p) => s + p.price, 0) / cluster.length;
      const supports = cluster.filter((p) => p.type === 'support').length;
      const resistances = cluster.filter((p) => p.type === 'resistance').length;
      const type = supports >= resistances ? 'support' : 'resistance';
      const times = cluster.map((p) => p.time);
      clusters.push({
        price: avgPrice,
        type,
        touches: cluster.length,
        firstSeen: Math.min(...times),
        lastSeen: Math.max(...times),
      });
      cluster = [sorted[i]];
    }
  }
  // Flush last cluster
  if (cluster.length > 0) {
    const avgPrice = cluster.reduce((s, p) => s + p.price, 0) / cluster.length;
    const supports = cluster.filter((p) => p.type === 'support').length;
    const resistances = cluster.filter((p) => p.type === 'resistance').length;
    const type = supports >= resistances ? 'support' : 'resistance';
    const times = cluster.map((p) => p.time);
    clusters.push({
      price: avgPrice,
      type,
      touches: cluster.length,
      firstSeen: Math.min(...times),
      lastSeen: Math.max(...times),
    });
  }

  // Score by touches * recency weight
  const lastTime = bars[bars.length - 1].time;
  const timeSpan = lastTime - bars[0].time || 1;
  const scored = clusters.map((c) => ({
    ...c,
    score: c.touches * (1 + (c.lastSeen - bars[0].time) / timeSpan),
  }));

  // Sort by score descending and take top N
  scored.sort((a, b) => b.score - a.score);
  return scored.slice(0, maxLevels).map(({ score, ...rest }) => rest);
}
