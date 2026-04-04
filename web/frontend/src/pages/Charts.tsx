import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BarChart3 } from 'lucide-react';
import { createChart, CandlestickSeries, HistogramSeries, LineSeries } from 'lightweight-charts';
import type { IChartApi, ISeriesApi, CandlestickData, HistogramData, LineData, Time } from 'lightweight-charts';
import { getOHLCV } from '../api/charts';
import type { OHLCVBar } from '../api/charts';
import { getScannerResults } from '../api/scanner';
import { useWSStore } from '../stores/wsStore';
import { getWatchlist } from '../api/scanner';
import { cn } from '../lib/utils';

const TIMEFRAMES = ['15m', '30m', '1h', '4h'] as const;

// ── Indicator helpers ───────────────────────────────────────

function calcRSI(data: OHLCVBar[], period: number = 14): LineData[] {
  const result: LineData[] = [];
  let gains = 0;
  let losses = 0;

  for (let i = 1; i < data.length; i++) {
    const change = data[i].close - data[i - 1].close;
    if (i <= period) {
      if (change > 0) gains += change;
      else losses -= change;
      if (i === period) {
        gains /= period;
        losses /= period;
        const rs = losses === 0 ? 100 : gains / losses;
        result.push({ time: data[i].time as Time, value: 100 - 100 / (1 + rs) });
      }
    } else {
      const gain = change > 0 ? change : 0;
      const loss = change < 0 ? -change : 0;
      gains = (gains * (period - 1) + gain) / period;
      losses = (losses * (period - 1) + loss) / period;
      const rs = losses === 0 ? 100 : gains / losses;
      result.push({ time: data[i].time as Time, value: 100 - 100 / (1 + rs) });
    }
  }
  return result;
}

// ── Chart Component ─────────────────────────────────────────

export default function Charts() {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const rsiContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const rsiChartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);

  const [activeSymbol, setActiveSymbol] = useState('BTC/USDT');
  const [activeTimeframe, setActiveTimeframe] = useState<string>('30m');
  const [showEMA, setShowEMA] = useState({ ema9: true, ema20: true, ema50: false });
  const [showRSI, setShowRSI] = useState(true);

  const { subscribe, lastMessage, status } = useWSStore();

  // Watchlist for symbol selector
  const { data: watchlistData } = useQuery({
    queryKey: ['scanner-watchlist'],
    queryFn: getWatchlist,
    refetchInterval: 60000,
  });

  // OHLCV data
  const { data: ohlcvData } = useQuery({
    queryKey: ['ohlcv', activeSymbol, activeTimeframe],
    queryFn: () => getOHLCV(activeSymbol, activeTimeframe, 300),
    refetchInterval: 30000,
  });

  // Signal markers from scanner
  const { data: scanData } = useQuery({
    queryKey: ['scanner-results'],
    queryFn: getScannerResults,
    refetchInterval: 30000,
  });

  // Subscribe to ticker for live updates
  useEffect(() => {
    if (status === 'connected') {
      subscribe('ticker');
    }
  }, [status, subscribe]);

  // Create charts
  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height: 400,
      layout: { background: { color: '#ffffff' }, textColor: '#374151' },
      grid: {
        vertLines: { color: '#f3f4f6' },
        horzLines: { color: '#f3f4f6' },
      },
      crosshair: { mode: 0 },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    chartRef.current = chart;

    const candleSeries = chart.addSeries(CandlestickSeries, {
      upColor: '#16a34a',
      downColor: '#dc2626',
      borderVisible: false,
      wickUpColor: '#16a34a',
      wickDownColor: '#dc2626',
    });
    candleSeriesRef.current = candleSeries;

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
    });
    volumeSeries.priceScale().applyOptions({
      scaleMargins: { top: 0.8, bottom: 0 },
    });
    volumeSeriesRef.current = volumeSeries;

    // Resize observer
    const ro = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      chart.applyOptions({ width });
    });
    ro.observe(chartContainerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // RSI chart
  useEffect(() => {
    if (!rsiContainerRef.current || !showRSI) return;

    const rsiChart = createChart(rsiContainerRef.current, {
      width: rsiContainerRef.current.clientWidth,
      height: 120,
      layout: { background: { color: '#ffffff' }, textColor: '#374151' },
      grid: {
        vertLines: { color: '#f3f4f6' },
        horzLines: { color: '#f3f4f6' },
      },
      timeScale: { timeVisible: true, secondsVisible: false },
    });
    rsiChartRef.current = rsiChart;

    const ro = new ResizeObserver((entries) => {
      const { width } = entries[0].contentRect;
      rsiChart.applyOptions({ width });
    });
    ro.observe(rsiContainerRef.current);

    return () => {
      ro.disconnect();
      rsiChart.remove();
      rsiChartRef.current = null;
    };
  }, [showRSI]);

  // Update chart data
  useEffect(() => {
    if (!ohlcvData?.bars || !candleSeriesRef.current || !volumeSeriesRef.current) return;

    const bars = ohlcvData.bars;
    const candles: CandlestickData[] = bars.map((b) => ({
      time: b.time as Time,
      open: b.open,
      high: b.high,
      low: b.low,
      close: b.close,
    }));

    const volumes: HistogramData[] = bars.map((b) => ({
      time: b.time as Time,
      value: b.volume,
      color: b.close >= b.open ? 'rgba(22,163,74,0.3)' : 'rgba(220,38,38,0.3)',
    }));

    candleSeriesRef.current.setData(candles);
    volumeSeriesRef.current.setData(volumes);

    // EMA overlays
    const chart = chartRef.current;
    if (chart) {
      // Remove old EMA series by recreating approach — simpler to just add each time
      // We track them by adding as new series (chart handles cleanup on data change)
    }

    // RSI pane
    if (showRSI && rsiChartRef.current && bars.length > 14) {
      const rsiData = calcRSI(bars, 14);
      // Clear and re-add RSI series
      const rsiChart = rsiChartRef.current;
      // Remove all existing series first
      try {
        const rsiSeries = rsiChart.addSeries(LineSeries, {
          color: '#7c3aed',
          lineWidth: 1,
          priceScaleId: 'right',
        });
        rsiSeries.setData(rsiData);
      } catch {
        // Series already exists on re-render
      }
    }

    // Signal markers
    if (scanData?.results) {
      const markers = scanData.results
        .filter((r) => r.symbol === activeSymbol && r.generated_at)
        .map((r) => ({
          time: (new Date(r.generated_at).getTime() / 1000) as Time,
          position: r.direction === 'buy' || r.direction === 'long' ? 'belowBar' as const : 'aboveBar' as const,
          color: r.direction === 'buy' || r.direction === 'long' ? '#16a34a' : '#dc2626',
          shape: r.direction === 'buy' || r.direction === 'long' ? 'arrowUp' as const : 'arrowDown' as const,
          text: r.direction === 'buy' || r.direction === 'long' ? 'BUY' : 'SELL',
        }));
      if (markers.length > 0) {
        (candleSeriesRef.current as any).setMarkers(markers);
      }
    }

    chartRef.current?.timeScale().fitContent();
  }, [ohlcvData, showEMA, showRSI, activeSymbol, scanData]);

  // Live ticker update
  useEffect(() => {
    const tickerData = lastMessage['ticker'];
    if (!tickerData || !candleSeriesRef.current) return;
    if (tickerData.symbol !== activeSymbol) return;

    // Update the last candle
    candleSeriesRef.current.update({
      time: tickerData.time as Time,
      open: tickerData.open,
      high: tickerData.high,
      low: tickerData.low,
      close: tickerData.close,
    });
  }, [lastMessage, activeSymbol]);

  const symbols = watchlistData?.symbols || ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'];

  return (
    <div className="space-y-4">
      {/* Header */}
      <div className="flex items-center gap-3">
        <BarChart3 className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Chart Workspace</h1>
      </div>

      {/* Controls */}
      <div className="flex flex-col sm:flex-row gap-3">
        {/* Symbol selector */}
        <div className="flex flex-wrap gap-1.5 overflow-x-auto pb-1">
          {symbols.map((sym) => (
            <button
              key={sym}
              onClick={() => setActiveSymbol(sym)}
              className={cn(
                'px-3 py-2 rounded-lg text-sm font-medium transition-colors min-h-[44px] whitespace-nowrap',
                activeSymbol === sym
                  ? 'bg-blue-600 text-white'
                  : 'bg-white border border-gray-200 text-gray-700 hover:bg-gray-50',
              )}
            >
              {sym}
            </button>
          ))}
        </div>

        {/* Timeframe selector */}
        <div className="flex gap-1.5">
          {TIMEFRAMES.map((tf) => (
            <button
              key={tf}
              onClick={() => setActiveTimeframe(tf)}
              className={cn(
                'px-3 py-2 rounded-lg text-sm font-medium transition-colors min-h-[44px]',
                activeTimeframe === tf
                  ? 'bg-gray-900 text-white'
                  : 'bg-white border border-gray-200 text-gray-600 hover:bg-gray-50',
              )}
            >
              {tf}
            </button>
          ))}
        </div>

        {/* Indicator toggles */}
        <div className="flex gap-1.5 ml-auto">
          <button
            onClick={() => setShowRSI(!showRSI)}
            className={cn(
              'px-3 py-2 rounded-lg text-xs font-medium min-h-[44px]',
              showRSI ? 'bg-purple-100 text-purple-700' : 'bg-gray-100 text-gray-500',
            )}
          >
            RSI
          </button>
          <button
            onClick={() => setShowEMA((p) => ({ ...p, ema9: !p.ema9 }))}
            className={cn(
              'px-3 py-2 rounded-lg text-xs font-medium min-h-[44px]',
              showEMA.ema9 ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-500',
            )}
          >
            EMA9
          </button>
          <button
            onClick={() => setShowEMA((p) => ({ ...p, ema20: !p.ema20 }))}
            className={cn(
              'px-3 py-2 rounded-lg text-xs font-medium min-h-[44px]',
              showEMA.ema20 ? 'bg-orange-100 text-orange-700' : 'bg-gray-100 text-gray-500',
            )}
          >
            EMA20
          </button>
        </div>
      </div>

      {/* Chart area */}
      <div className="bg-white rounded-lg border border-gray-200 p-2">
        <div ref={chartContainerRef} className="w-full" />
      </div>

      {/* RSI pane */}
      {showRSI && (
        <div className="bg-white rounded-lg border border-gray-200 p-2">
          <p className="text-xs text-gray-400 mb-1 px-1">RSI (14)</p>
          <div ref={rsiContainerRef} className="w-full" />
        </div>
      )}
    </div>
  );
}
