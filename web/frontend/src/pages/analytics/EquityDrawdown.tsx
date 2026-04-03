import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { createChart, LineSeries, AreaSeries } from 'lightweight-charts';
import type { IChartApi, Time } from 'lightweight-charts';
import { getEquityCurve, getDrawdownCurve, getRollingMetrics } from '../../api/analytics';
import { cn, formatUSD, formatPct } from '../../lib/utils';

const WINDOW_OPTIONS = [10, 20, 50] as const;

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className="text-sm font-mono font-semibold text-gray-900">{value}</p>
    </div>
  );
}

export default function EquityDrawdown() {
  const equityRef = useRef<HTMLDivElement>(null);
  const ddRef = useRef<HTMLDivElement>(null);
  const rollingRef = useRef<HTMLDivElement>(null);
  const equityChart = useRef<IChartApi | null>(null);
  const ddChart = useRef<IChartApi | null>(null);
  const rollingChart = useRef<IChartApi | null>(null);

  const [rollingWindow, setRollingWindow] = useState<number>(20);

  const { data: curveData } = useQuery({
    queryKey: ['equity-curve'],
    queryFn: getEquityCurve,
    refetchInterval: 30000,
  });

  const { data: ddData } = useQuery({
    queryKey: ['drawdown-curve'],
    queryFn: getDrawdownCurve,
    refetchInterval: 30000,
  });

  const { data: rollingData } = useQuery({
    queryKey: ['rolling-metrics', rollingWindow],
    queryFn: () => getRollingMetrics(rollingWindow),
    refetchInterval: 30000,
  });

  // ── Equity curve with green/red area fill ──
  useEffect(() => {
    if (!equityRef.current) return;
    const chart = createChart(equityRef.current, {
      width: equityRef.current.clientWidth,
      height: 280,
      layout: { background: { color: '#fff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#e5e7eb' },
    });
    equityChart.current = chart;
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    ro.observe(equityRef.current);
    return () => { ro.disconnect(); chart.remove(); equityChart.current = null; };
  }, []);

  useEffect(() => {
    if (!equityChart.current || !curveData?.points?.length) return;
    const initial = curveData.initial_capital ?? curveData.points[0]?.capital ?? 0;
    const series = equityChart.current.addSeries(AreaSeries, {
      lineColor: '#2563eb',
      lineWidth: 2,
      topColor: 'rgba(37,99,235,0.3)',
      bottomColor: 'rgba(37,99,235,0.02)',
    });
    series.setData(curveData.points.map((p) => ({ time: p.time as Time, value: p.capital })));
    // Add baseline at initial capital
    series.createPriceLine({ price: initial, color: '#9ca3af', lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: '' });
    equityChart.current.timeScale().fitContent();
    return () => { try { equityChart.current?.removeSeries(series); } catch {} };
  }, [curveData]);

  // ── Drawdown curve (inverted, red area) ──
  useEffect(() => {
    if (!ddRef.current) return;
    const chart = createChart(ddRef.current, {
      width: ddRef.current.clientWidth,
      height: 180,
      layout: { background: { color: '#fff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#e5e7eb' },
    });
    ddChart.current = chart;
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    ro.observe(ddRef.current);
    return () => { ro.disconnect(); chart.remove(); ddChart.current = null; };
  }, []);

  useEffect(() => {
    if (!ddChart.current || !ddData?.points?.length) return;
    const series = ddChart.current.addSeries(AreaSeries, {
      lineColor: '#dc2626',
      lineWidth: 2,
      topColor: 'rgba(220,38,38,0.02)',
      bottomColor: 'rgba(220,38,38,0.3)',
      invertFilledArea: true,
    });
    series.setData(ddData.points.map((p) => ({
      time: p.time as Time,
      value: -Math.abs(p.drawdown_pct),
    })));
    ddChart.current.timeScale().fitContent();
    return () => { try { ddChart.current?.removeSeries(series); } catch {} };
  }, [ddData]);

  // ── Rolling metrics chart (dual line: WR + PF) ──
  useEffect(() => {
    if (!rollingRef.current) return;
    const chart = createChart(rollingRef.current, {
      width: rollingRef.current.clientWidth,
      height: 240,
      layout: { background: { color: '#fff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { borderColor: '#e5e7eb' },
    });
    rollingChart.current = chart;
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    ro.observe(rollingRef.current);
    return () => { ro.disconnect(); chart.remove(); rollingChart.current = null; };
  }, []);

  useEffect(() => {
    if (!rollingChart.current || !rollingData?.points?.length) return;
    const wrSeries = rollingChart.current.addSeries(LineSeries, {
      color: '#2563eb',
      lineWidth: 2,
      title: 'Win Rate %',
    });
    wrSeries.setData(rollingData.points.map((p) => ({
      time: p.time as Time,
      value: p.rolling_wr,
    })));
    const pfSeries = rollingChart.current.addSeries(LineSeries, {
      color: '#16a34a',
      lineWidth: 2,
      title: 'Profit Factor',
      priceScaleId: 'pf',
    });
    pfSeries.setData(rollingData.points.map((p) => ({
      time: p.time as Time,
      value: p.rolling_pf,
    })));
    rollingChart.current.applyOptions({
      rightPriceScale: { visible: true },
    });
    rollingChart.current.timeScale().fitContent();
    return () => {
      try {
        rollingChart.current?.removeSeries(wrSeries);
        rollingChart.current?.removeSeries(pfSeries);
      } catch {}
    };
  }, [rollingData]);

  // Compute stats from drawdown data
  const maxDD = ddData?.points?.length
    ? Math.max(...ddData.points.map((p) => Math.abs(p.drawdown_pct)))
    : 0;
  const peakCapital = ddData?.points?.length
    ? Math.max(...ddData.points.map((p) => p.peak_capital))
    : 0;
  const currentDD = ddData?.points?.length
    ? Math.abs(ddData.points[ddData.points.length - 1].drawdown_pct)
    : 0;

  return (
    <div className="space-y-4">
      {/* Statistics panel */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Peak Capital" value={formatUSD(peakCapital)} />
        <StatCard label="Current DD" value={formatPct(-currentDD)} />
        <StatCard label="Max DD" value={formatPct(-maxDD)} />
        <StatCard label="Total Trades" value={String(curveData?.points?.length ?? 0)} />
      </div>

      {/* Equity Curve */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Equity Curve</p>
        <div ref={equityRef} className="w-full" />
        {(!curveData?.points || curveData.points.length === 0) && (
          <p className="text-sm text-gray-400 text-center py-8">No trade data yet</p>
        )}
      </div>

      {/* Drawdown Curve */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Drawdown Curve</p>
        <div ref={ddRef} className="w-full" />
        {(!ddData?.points || ddData.points.length === 0) && (
          <p className="text-sm text-gray-400 text-center py-8">No drawdown data yet</p>
        )}
      </div>

      {/* Rolling Metrics */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <div className="flex items-center justify-between mb-2">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">
            Rolling Metrics (window: {rollingWindow})
          </p>
          <div className="flex gap-1">
            {WINDOW_OPTIONS.map((w) => (
              <button
                key={w}
                onClick={() => setRollingWindow(w)}
                className={cn(
                  'px-2 py-1 text-xs rounded font-medium transition-colors min-h-[44px] min-w-[44px]',
                  rollingWindow === w
                    ? 'bg-blue-600 text-white'
                    : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                )}
              >
                {w}
              </button>
            ))}
          </div>
        </div>
        <div className="flex gap-4 text-xs text-gray-400 mb-2">
          <span className="flex items-center gap-1">
            <span className="inline-block w-3 h-0.5 bg-blue-600" /> Win Rate %
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block w-3 h-0.5 bg-green-600" /> Profit Factor
          </span>
        </div>
        <div ref={rollingRef} className="w-full" />
        {(!rollingData?.points || rollingData.points.length === 0) && (
          <p className="text-sm text-gray-400 text-center py-8">Not enough trades for rolling metrics</p>
        )}
      </div>
    </div>
  );
}
