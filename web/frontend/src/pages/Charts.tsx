import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BarChart3, ChevronDown, Info } from 'lucide-react';
import { createChart, CandlestickSeries, HistogramSeries, LineSeries, AreaSeries } from 'lightweight-charts';
import type { IChartApi, ISeriesApi, CandlestickData, HistogramData, LineData, Time } from 'lightweight-charts';
import { getOHLCV } from '../api/charts';
import type { OHLCVBar } from '../api/charts';
import { getExchanges, getTradableAssets } from '../api/exchanges';
import { cn } from '../lib/utils';
import {
  calcSMA, calcRSI, calcMACD, calcBollingerBands, calcSupportResistance,
  type Bar, type Point, type SRLevel,
} from '../lib/indicators';

// ── Constants ─────────────────────────────────────────────────

const TIMEFRAMES = [
  { value: '15m', label: '15m' },
  { value: '30m', label: '30m' },
  { value: '1h',  label: '1h' },
  { value: '4h',  label: '4h' },
  { value: '1d',  label: '1D' },
  { value: '1w',  label: '1W' },
] as const;

interface IndicatorDef {
  key: string;
  label: string;
  category: string;
  defaultOn: boolean;
}

const INDICATORS: IndicatorDef[] = [
  { key: 'ma50',    label: '50 MA',               category: 'Trend',        defaultOn: true },
  { key: 'ma200',   label: '200 MA',              category: 'Trend',        defaultOn: false },
  { key: 'rsi',     label: 'RSI',                 category: 'Momentum',     defaultOn: true },
  { key: 'macd',    label: 'MACD',                category: 'Confirmation', defaultOn: false },
  { key: 'bb',      label: 'Bollinger Bands',     category: 'Volatility',   defaultOn: false },
  { key: 'sr',      label: 'Support / Resistance',category: 'Structure',    defaultOn: false },
];

const CATEGORY_COLORS: Record<string, string> = {
  Trend: 'border-blue-200 bg-blue-50/60',
  Momentum: 'border-purple-200 bg-purple-50/60',
  Confirmation: 'border-amber-200 bg-amber-50/60',
  Volatility: 'border-teal-200 bg-teal-50/60',
  Structure: 'border-rose-200 bg-rose-50/60',
};

const CHART_OPTS = {
  layout: { background: { color: '#ffffff' }, textColor: '#64748b', fontFamily: 'Inter, system-ui, sans-serif' },
  grid: { vertLines: { color: '#f1f5f9' }, horzLines: { color: '#f1f5f9' } },
  crosshair: { mode: 0 as const },
  timeScale: { timeVisible: true, secondsVisible: false, borderColor: '#e2e8f0' },
  rightPriceScale: { borderColor: '#e2e8f0' },
};

// ── Helpers ───────────────────────────────────────────────────

function toTime(t: number): Time { return t as Time; }

function dedup(pts: Point[]): Point[] {
  const seen = new Set<number>();
  return pts.filter((p) => { if (seen.has(p.time)) return false; seen.add(p.time); return true; });
}

// ── Chart Component ───────────────────────────────────────────

export default function Charts() {
  // ── refs ──
  const mainRef = useRef<HTMLDivElement>(null);
  const rsiRef = useRef<HTMLDivElement>(null);
  const macdRef = useRef<HTMLDivElement>(null);
  const mainChart = useRef<IChartApi | null>(null);
  const rsiChart = useRef<IChartApi | null>(null);
  const macdChart = useRef<IChartApi | null>(null);

  // Series refs for cleanup
  const seriesRefs = useRef<Record<string, ISeriesApi<any> | ISeriesApi<any>[]>>({});

  // ── state ──
  const [symbol, setSymbol] = useState('BTC/USDT');
  const [timeframe, setTimeframe] = useState('1h');
  const [indicators, setIndicators] = useState<Record<string, boolean>>(() => {
    const m: Record<string, boolean> = {};
    INDICATORS.forEach((i) => { m[i.key] = i.defaultOn; });
    return m;
  });

  // ── data queries ──
  const { data: exchangesData } = useQuery({
    queryKey: ['exchanges-list'],
    queryFn: getExchanges,
    staleTime: 60000,
  });

  const activeExchange = exchangesData?.find((e) => e.is_active);

  const { data: tradableData } = useQuery({
    queryKey: ['tradable-assets', activeExchange?.id],
    queryFn: () => getTradableAssets(activeExchange!.id),
    enabled: !!activeExchange,
    staleTime: 60000,
  });

  const symbols = tradableData?.symbols || [];

  // Request more bars for longer MAs
  const barLimit = indicators.ma200 ? 500 : 300;

  const { data: ohlcvData, isLoading, error } = useQuery({
    queryKey: ['ohlcv', symbol, timeframe, barLimit],
    queryFn: () => getOHLCV(symbol, timeframe, barLimit),
    refetchInterval: 30000,
  });

  const bars: Bar[] = ohlcvData?.bars || [];

  // ── indicator toggle ──
  const toggle = useCallback((key: string) => {
    setIndicators((prev) => ({ ...prev, [key]: !prev[key] }));
  }, []);

  // ── Single unified effect: create charts + render all data ──
  // Re-runs when bars or indicator toggles change. Destroys and recreates cleanly.
  useEffect(() => {
    if (!mainRef.current || bars.length === 0) return;

    // Create main chart
    const chart = createChart(mainRef.current, { ...CHART_OPTS, width: mainRef.current.clientWidth, height: 480 });
    mainChart.current = chart;
    const mainRO = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    mainRO.observe(mainRef.current);

    // Track all observers for cleanup
    const observers: ResizeObserver[] = [mainRO];

    // ── Candlesticks ──
    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#22c55e', downColor: '#ef4444',
      borderVisible: false, wickUpColor: '#22c55e', wickDownColor: '#ef4444',
    });
    candleSeries.setData(bars.map((b) => ({
      time: toTime(b.time), open: b.open, high: b.high, low: b.low, close: b.close,
    })));
    seriesRefs.current['candle'] = candleSeries;

    // ── Volume ──
    const volSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' }, priceScaleId: 'vol',
    });
    volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
    volSeries.setData(bars.map((b) => ({
      time: toTime(b.time), value: b.volume,
      color: b.close >= b.open ? 'rgba(34,197,94,0.15)' : 'rgba(239,68,68,0.15)',
    })));
    seriesRefs.current['volume'] = volSeries;

    // ── 50 MA ──
    if (indicators.ma50) {
      const pts = dedup(calcSMA(bars, 50));
      if (pts.length > 0) {
        const s = chart.addSeries(LineSeries, {
          color: '#3b82f6', lineWidth: 2, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData(pts.map((p) => ({ time: toTime(p.time), value: p.value })));
        seriesRefs.current['ma50'] = s;
      }
    }

    // ── 200 MA ──
    if (indicators.ma200) {
      const pts = dedup(calcSMA(bars, 200));
      if (pts.length > 0) {
        const s = chart.addSeries(LineSeries, {
          color: '#f59e0b', lineWidth: 2, priceLineVisible: false,
          lastValueVisible: false, crosshairMarkerVisible: false,
        });
        s.setData(pts.map((p) => ({ time: toTime(p.time), value: p.value })));
        seriesRefs.current['ma200'] = s;
      }
    }

    // ── Bollinger Bands ──
    if (indicators.bb) {
      const bb = calcBollingerBands(bars, 20, 2);
      if (bb.upper.length > 0) {
        const sUpper = chart.addSeries(LineSeries, {
          color: 'rgba(139,92,246,0.6)', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        sUpper.setData(dedup(bb.upper).map((p) => ({ time: toTime(p.time), value: p.value })));

        const sMiddle = chart.addSeries(LineSeries, {
          color: 'rgba(139,92,246,0.4)', lineWidth: 1, lineStyle: 1,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        sMiddle.setData(dedup(bb.middle).map((p) => ({ time: toTime(p.time), value: p.value })));

        const sLower = chart.addSeries(LineSeries, {
          color: 'rgba(139,92,246,0.6)', lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        sLower.setData(dedup(bb.lower).map((p) => ({ time: toTime(p.time), value: p.value })));

        seriesRefs.current['bb'] = [sUpper, sMiddle, sLower];
      }
    }

    // ── Support / Resistance ──
    if (indicators.sr) {
      const levels = calcSupportResistance(bars, 5, 0.5, 5);
      levels.forEach((lvl) => {
        const s = chart.addSeries(LineSeries, {
          color: lvl.type === 'support' ? 'rgba(34,197,94,0.7)' : 'rgba(239,68,68,0.7)',
          lineWidth: 1, lineStyle: 2,
          priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false,
        });
        const first = bars[0].time;
        const last = bars[bars.length - 1].time;
        s.setData([
          { time: toTime(first), value: lvl.price },
          { time: toTime(last), value: lvl.price },
        ]);
      });
    }

    chart.timeScale().fitContent();

    // Cleanup: destroy main chart and observers on re-render
    return () => {
      observers.forEach((ro) => ro.disconnect());
      try { chart.remove(); } catch {}
      mainChart.current = null;
    };
  }, [bars, indicators.ma50, indicators.ma200, indicators.bb, indicators.sr]);

  // ── RSI pane (useLayoutEffect — runs synchronously after DOM commit) ──
  useLayoutEffect(() => {
    if (!indicators.rsi || !rsiRef.current || bars.length === 0) return;
    const rc = createChart(rsiRef.current, { ...CHART_OPTS, width: rsiRef.current.clientWidth, height: 150 });
    const ro = new ResizeObserver(([e]) => rc.applyOptions({ width: e.contentRect.width }));
    ro.observe(rsiRef.current);

    const rsiData = dedup(calcRSI(bars, 14));
    if (rsiData.length > 0) {
      const s = rc.addSeries(LineSeries, { color: '#8b5cf6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
      s.setData(rsiData.map((p) => ({ time: toTime(p.time), value: p.value })));
      s.createPriceLine({ price: 70, color: '#ef4444', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
      s.createPriceLine({ price: 50, color: '#94a3b8', lineWidth: 1, lineStyle: 1, axisLabelVisible: false, title: '' });
      s.createPriceLine({ price: 30, color: '#22c55e', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
      rc.timeScale().fitContent();
    }

    return () => { ro.disconnect(); try { rc.remove(); } catch {} };
  }, [bars, indicators.rsi]);

  // ── MACD pane (useLayoutEffect — runs synchronously after DOM commit) ──
  useLayoutEffect(() => {
    if (!indicators.macd || !macdRef.current || bars.length === 0) return;
    const mc = createChart(macdRef.current, { ...CHART_OPTS, width: macdRef.current.clientWidth, height: 160 });
    const ro = new ResizeObserver(([e]) => mc.applyOptions({ width: e.contentRect.width }));
    ro.observe(macdRef.current);

    const macd = calcMACD(bars, 12, 26, 9);
    if (macd.macdLine.length > 0) {
      const sLine = mc.addSeries(LineSeries, { color: '#3b82f6', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
      sLine.setData(dedup(macd.macdLine).map((p) => ({ time: toTime(p.time), value: p.value })));

      if (macd.signalLine.length > 0) {
        const sSig = mc.addSeries(LineSeries, { color: '#f97316', lineWidth: 1.5, priceLineVisible: false, lastValueVisible: true });
        sSig.setData(dedup(macd.signalLine).map((p) => ({ time: toTime(p.time), value: p.value })));
      }

      if (macd.histogram.length > 0) {
        const sHist = mc.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false });
        sHist.setData(dedup(macd.histogram).map((p) => ({
          time: toTime(p.time), value: p.value,
          color: p.value >= 0 ? 'rgba(34,197,94,0.5)' : 'rgba(239,68,68,0.5)',
        })));
      }

      const sZero = mc.addSeries(LineSeries, { color: '#94a3b8', lineWidth: 1, lineStyle: 1, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
      sZero.setData([{ time: toTime(bars[0].time), value: 0 }, { time: toTime(bars[bars.length - 1].time), value: 0 }]);
      mc.timeScale().fitContent();
    }

    return () => { ro.disconnect(); try { mc.remove(); } catch {} };
  }, [bars, indicators.macd]);

  // ── Insufficient data warnings ──
  const warnings: string[] = [];
  if (bars.length > 0 && bars.length < 50 && indicators.ma50) warnings.push('Insufficient data for 50 MA');
  if (bars.length > 0 && bars.length < 200 && indicators.ma200) warnings.push('Insufficient data for 200 MA — try a shorter timeframe or increase bar count');
  if (bars.length > 0 && bars.length < 26 && indicators.macd) warnings.push('Insufficient data for MACD');

  // ── Group indicators by category ──
  const categories = Array.from(new Set(INDICATORS.map((i) => i.category)));

  return (
    <div className="space-y-4">
      {/* ── Header ── */}
      <div className="flex items-center gap-3">
        <BarChart3 className="w-5 h-5 text-blue-500" />
        <h1 className="text-xl font-bold text-gray-900">Chart Workspace</h1>
      </div>

      {/* ── Controls Bar ── */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <div className="flex flex-col lg:flex-row gap-4">
          {/* Asset + Timeframe selectors */}
          <div className="flex gap-3 items-center">
            {/* Asset dropdown */}
            <div className="relative">
              <label className="block text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-1">Asset</label>
              <select
                value={symbol}
                onChange={(e) => setSymbol(e.target.value)}
                className="appearance-none bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 pr-8 text-sm font-semibold text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 min-w-[140px]"
              >
                {symbols.length === 0 && <option value={symbol}>{symbol}</option>}
                {symbols.map((s) => (
                  <option key={s} value={s}>{s}</option>
                ))}
              </select>
              <ChevronDown className="absolute right-2.5 bottom-2.5 w-4 h-4 text-gray-400 pointer-events-none" />
            </div>

            {/* Timeframe dropdown */}
            <div className="relative">
              <label className="block text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-1">Timeframe</label>
              <select
                value={timeframe}
                onChange={(e) => setTimeframe(e.target.value)}
                className="appearance-none bg-gray-50 border border-gray-200 rounded-lg px-4 py-2 pr-8 text-sm font-semibold text-gray-900 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 min-w-[100px]"
              >
                {TIMEFRAMES.map((tf) => (
                  <option key={tf.value} value={tf.value}>{tf.label}</option>
                ))}
              </select>
              <ChevronDown className="absolute right-2.5 bottom-2.5 w-4 h-4 text-gray-400 pointer-events-none" />
            </div>
          </div>

          {/* Indicator toggles grouped by category */}
          <div className="flex flex-wrap gap-2 items-end flex-1">
            {categories.map((cat) => (
              <div key={cat} className={cn('flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5', CATEGORY_COLORS[cat] || 'border-gray-200 bg-gray-50')}>
                <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wider mr-1">{cat}</span>
                {INDICATORS.filter((i) => i.category === cat).map((ind) => (
                  <button
                    key={ind.key}
                    onClick={() => toggle(ind.key)}
                    className={cn(
                      'px-2.5 py-1 rounded-md text-xs font-semibold transition-all duration-150',
                      indicators[ind.key]
                        ? 'bg-white text-gray-900 shadow-sm border border-gray-200'
                        : 'text-gray-400 hover:text-gray-600',
                    )}
                  >
                    {ind.label}
                  </button>
                ))}
              </div>
            ))}
          </div>
        </div>

        {/* Warnings */}
        {warnings.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-2">
            {warnings.map((w, i) => (
              <span key={i} className="inline-flex items-center gap-1 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md px-2 py-1">
                <Info className="w-3 h-3" /> {w}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* ── Loading / Error / Empty states ── */}
      {isLoading && (
        <div className="bg-white rounded-xl border border-gray-200 p-16 text-center">
          <div className="animate-pulse text-gray-400 text-sm">Loading chart data...</div>
        </div>
      )}
      {error && (
        <div className="bg-red-50 rounded-xl border border-red-200 p-6 text-center text-sm text-red-700">
          Failed to load chart data. Check your exchange connection.
        </div>
      )}
      {!isLoading && !error && symbols.length === 0 && (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
          <BarChart3 className="w-10 h-10 text-gray-200 mx-auto mb-3" />
          <p className="text-sm text-gray-500 font-medium">No tradable assets configured</p>
          <p className="text-xs text-gray-400 mt-1">Go to Asset Management to enable tradable pairs</p>
        </div>
      )}

      {/* ── Unified Chart Container (TradingView-style stacked panes) ── */}
      {!isLoading && bars.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
          {/* Price chart header */}
          <div className="px-4 py-2 border-b border-gray-100 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <span className="text-sm font-bold text-gray-900">{symbol}</span>
              <span className="text-xs text-gray-400 font-medium">{TIMEFRAMES.find(t => t.value === timeframe)?.label}</span>
              <span className="text-xs font-mono text-gray-500">
                {bars[bars.length - 1].close.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
              </span>
            </div>
            <div className="flex gap-3 text-[10px] text-gray-400">
              {indicators.ma50 && <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-blue-500 rounded" /> 50 MA</span>}
              {indicators.ma200 && <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-amber-500 rounded" /> 200 MA</span>}
              {indicators.bb && <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-violet-500 rounded" /> BB</span>}
              {indicators.sr && (
                <>
                  <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-green-500 rounded" /> S</span>
                  <span className="flex items-center gap-1"><span className="w-3 h-0.5 bg-red-500 rounded" /> R</span>
                </>
              )}
            </div>
          </div>

          {/* Main price chart */}
          <div ref={mainRef} className="w-full" />

          {/* RSI sub-pane */}
          {indicators.rsi && (
            <div>
              <div className="px-3 py-1 border-t border-gray-200 flex items-center justify-between bg-gray-50/50">
                <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">RSI (14)</span>
                <div className="flex gap-2 text-[10px] text-gray-400">
                  <span className="text-red-400">70</span>
                  <span className="text-green-400">30</span>
                </div>
              </div>
              <div ref={rsiRef} className="w-full" />
            </div>
          )}

          {/* MACD sub-pane */}
          {indicators.macd && (
            <div>
              <div className="px-3 py-1 border-t border-gray-200 flex items-center justify-between bg-gray-50/50">
                <span className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider">MACD (12, 26, 9)</span>
                <div className="flex gap-2 text-[10px] text-gray-400">
                  <span className="flex items-center gap-1"><span className="w-2 h-0.5 bg-blue-500 rounded" /> MACD</span>
                  <span className="flex items-center gap-1"><span className="w-2 h-0.5 bg-orange-500 rounded" /> Signal</span>
                </div>
              </div>
              <div ref={macdRef} className="w-full" />
            </div>
          )}
        </div>
      )}

      {/* ── Educational Section ── */}
      <EducationalSection />
    </div>
  );
}

// ── Educational Section Component ─────────────────────────────

function EducationalSection() {
  const [open, setOpen] = useState<string | null>(null);

  const entries = [
    {
      id: 'ma',
      title: '50 MA / 200 MA',
      category: 'Trend',
      measures: 'Average closing price over the last 50 or 200 periods, smoothing out short-term noise to reveal the underlying trend direction.',
      matters: 'Moving averages are among the most widely watched indicators in all markets. The 200 MA is considered the dividing line between long-term bullish and bearish structure. The 50 MA serves as a medium-term trend reference and dynamic support/resistance.',
      calculation: 'MA(n) = Sum of last n closing prices / n. Both use Simple Moving Average (SMA) to match the conventional interpretation of "50-day" and "200-day" averages.',
      interpretation: [
        'Price above the 200 MA generally indicates stronger bullish structure',
        'Price below the 200 MA generally indicates weaker bearish structure',
        'The 50 MA is useful for medium-term support/resistance and trend alignment',
        'A "Golden Cross" (50 MA crossing above 200 MA) can signal a major bullish trend shift',
        'A "Death Cross" (50 MA crossing below 200 MA) can signal a major bearish trend shift',
      ],
      warning: 'Moving average crossovers lag by nature and should not be used in isolation. They work best as trend context, not as standalone entry/exit signals.',
    },
    {
      id: 'rsi',
      title: 'RSI (Relative Strength Index)',
      category: 'Momentum',
      measures: 'The speed and magnitude of recent price changes, expressed as a value between 0 and 100.',
      matters: 'RSI helps identify when momentum is stretched to extremes (overbought or oversold) and can reveal divergences between price direction and underlying momentum strength.',
      calculation: 'RS = Average Gain / Average Loss over 14 periods (Wilder smoothing). RSI = 100 - (100 / (1 + RS)).',
      interpretation: [
        'RSI above 70 suggests overbought conditions or stretched upside momentum',
        'RSI below 30 suggests oversold conditions or stretched downside momentum',
        'RSI around 50 helps assess bullish vs bearish momentum balance',
        'Bullish divergence: price makes a lower low while RSI makes a higher low',
        'Bearish divergence: price makes a higher high while RSI makes a lower high',
      ],
      warning: 'RSI should not be used alone. In strong trends, RSI can remain overbought or oversold for extended periods. It is strongest when combined with price structure and trend context.',
    },
    {
      id: 'macd',
      title: 'MACD (Moving Average Convergence Divergence)',
      category: 'Confirmation',
      measures: 'The relationship between two exponential moving averages (12 and 26 period), with a signal line (9 period EMA of MACD) and histogram showing their difference.',
      matters: 'MACD combines trend-following and momentum confirmation into a single indicator. It is especially useful for confirming the direction and strength of a move rather than predicting reversals.',
      calculation: 'MACD Line = EMA(12) - EMA(26). Signal Line = EMA(9) of MACD Line. Histogram = MACD Line - Signal Line.',
      interpretation: [
        'Bullish crossover: MACD Line crosses above Signal Line',
        'Bearish crossover: MACD Line crosses below Signal Line',
        'Histogram expansion indicates strengthening momentum',
        'Histogram contraction indicates weakening momentum',
        'Divergence between MACD and price can suggest weakening trend strength',
      ],
      warning: 'MACD is a lagging indicator and is best used as confirmation rather than a standalone reversal call. False crossovers are common in ranging markets.',
    },
    {
      id: 'bb',
      title: 'Bollinger Bands',
      category: 'Volatility',
      measures: 'Price volatility relative to a moving average, using standard deviation bands that expand and contract dynamically.',
      matters: 'Bollinger Bands help identify periods of high and low volatility, potential mean reversion points, and volatility breakout setups. A band squeeze often precedes a significant price move.',
      calculation: 'Middle Band = 20-period SMA. Upper Band = Middle + (2 x StdDev). Lower Band = Middle - (2 x StdDev).',
      interpretation: [
        'Upper band contact can indicate stretched upside conditions',
        'Lower band contact can indicate stretched downside conditions',
        'A band squeeze (narrow bands) signals volatility compression and potential for a larger move',
        'Bands expanding indicates rising volatility',
        'Price consistently riding the upper band can indicate strong bullish momentum, not necessarily a reversal',
      ],
      warning: 'Touching a band alone is not a reversal signal; context matters. Use with momentum and structure confirmation. In strong trends, price can "walk the band" for extended periods.',
    },
    {
      id: 'sr',
      title: 'Support / Resistance',
      category: 'Structure',
      measures: 'Key price levels where buying demand (support) or selling pressure (resistance) has historically been strong enough to influence price direction.',
      matters: 'Support and resistance are foundational to technical analysis. They define the structural boundaries of price action and are critical for identifying entries, exits, stop placement, and invalidation zones.',
      calculation: 'Identified using pivot-based swing detection: bars where the high (resistance) or low (support) is the most extreme within a +-5 bar window. Nearby levels are clustered and scored by number of touches and recency.',
      interpretation: [
        'Support is a price area where demand has historically slowed or reversed a decline',
        'Resistance is a price area where supply has historically slowed or reversed an advance',
        'The more times a level is respected, the more significant it becomes',
        'Former resistance can become support after a breakout, and vice versa (role reversal)',
        'These are zones, not exact single-price absolutes',
      ],
      warning: 'Support and resistance levels are probabilistic, not guarantees. They should be used in conjunction with trend, momentum, and volume confirmation. Breakouts and breakdowns invalidate prior levels.',
    },
  ];

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <Info className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">Indicator Reference Guide</h2>
      </div>

      <div className="divide-y divide-gray-100">
        {entries.map((entry) => (
          <div key={entry.id}>
            <button
              onClick={() => setOpen(open === entry.id ? null : entry.id)}
              className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
            >
              <div className="flex items-center gap-3">
                <span className={cn(
                  'px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider',
                  entry.category === 'Trend' ? 'bg-blue-50 text-blue-600' :
                  entry.category === 'Momentum' ? 'bg-purple-50 text-purple-600' :
                  entry.category === 'Confirmation' ? 'bg-amber-50 text-amber-600' :
                  entry.category === 'Volatility' ? 'bg-teal-50 text-teal-600' :
                  'bg-rose-50 text-rose-600'
                )}>
                  {entry.category}
                </span>
                <span className="text-sm font-semibold text-gray-900">{entry.title}</span>
              </div>
              <ChevronDown className={cn(
                'w-4 h-4 text-gray-400 transition-transform duration-200',
                open === entry.id && 'rotate-180',
              )} />
            </button>

            {open === entry.id && (
              <div className="px-5 pb-5 space-y-4 animate-in fade-in duration-200">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">What it measures</h4>
                    <p className="text-sm text-gray-700 leading-relaxed">{entry.measures}</p>
                  </div>
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Why it matters</h4>
                    <p className="text-sm text-gray-700 leading-relaxed">{entry.matters}</p>
                  </div>
                </div>

                <div>
                  <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Calculation</h4>
                  <p className="text-sm text-gray-600 font-mono bg-gray-50 rounded-lg px-3 py-2">{entry.calculation}</p>
                </div>

                <div>
                  <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">How to interpret</h4>
                  <ul className="space-y-1.5">
                    {entry.interpretation.map((point, i) => (
                      <li key={i} className="flex gap-2 text-sm text-gray-700">
                        <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                        {point}
                      </li>
                    ))}
                  </ul>
                </div>

                <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3">
                  <p className="text-xs font-semibold text-amber-800 mb-1">Caution</p>
                  <p className="text-sm text-amber-700">{entry.warning}</p>
                </div>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
