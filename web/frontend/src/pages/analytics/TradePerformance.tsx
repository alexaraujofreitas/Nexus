import { useEffect, useRef } from 'react';
import { useQuery } from '@tanstack/react-query';
import { createChart, HistogramSeries } from 'lightweight-charts';
import type { IChartApi, Time } from 'lightweight-charts';
import {
  getTradeDistribution,
  getRDistribution,
  getDurationAnalysis,
  getPerformanceMetrics,
} from '../../api/analytics';
import { cn, formatUSD } from '../../lib/utils';

function StatCard({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-3">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={cn('text-sm font-mono font-semibold', color ?? 'text-gray-900')}>{value}</p>
    </div>
  );
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

export default function TradePerformance() {
  const pnlRef = useRef<HTMLDivElement>(null);
  const rDistRef = useRef<HTMLDivElement>(null);
  const pnlChart = useRef<IChartApi | null>(null);
  const rDistChart = useRef<IChartApi | null>(null);

  const { data: distData } = useQuery({
    queryKey: ['trade-dist'],
    queryFn: getTradeDistribution,
    refetchInterval: 60000,
  });

  const { data: rDist } = useQuery({
    queryKey: ['r-distribution'],
    queryFn: getRDistribution,
    refetchInterval: 60000,
  });

  const { data: durData } = useQuery({
    queryKey: ['duration-analysis'],
    queryFn: getDurationAnalysis,
    refetchInterval: 60000,
  });

  const { data: metrics } = useQuery({
    queryKey: ['perf-metrics'],
    queryFn: getPerformanceMetrics,
    refetchInterval: 30000,
  });

  // ── PnL Distribution chart ──
  useEffect(() => {
    if (!pnlRef.current) return;
    const chart = createChart(pnlRef.current, {
      width: pnlRef.current.clientWidth,
      height: 220,
      layout: { background: { color: '#fff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
    });
    pnlChart.current = chart;
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    ro.observe(pnlRef.current);
    return () => { ro.disconnect(); chart.remove(); pnlChart.current = null; };
  }, []);

  useEffect(() => {
    if (!pnlChart.current || !distData?.buckets?.length) return;
    const series = pnlChart.current.addSeries(HistogramSeries, {});
    series.setData(distData.buckets.map((b, i) => ({
      time: (i + 1) as unknown as Time,
      value: b.count,
      color: b.range_min >= 0 ? 'rgba(22,163,74,0.6)' : 'rgba(220,38,38,0.6)',
    })));
    return () => { try { pnlChart.current?.removeSeries(series); } catch {} };
  }, [distData]);

  // ── R-multiple Distribution chart ──
  useEffect(() => {
    if (!rDistRef.current) return;
    const chart = createChart(rDistRef.current, {
      width: rDistRef.current.clientWidth,
      height: 220,
      layout: { background: { color: '#fff' }, textColor: '#374151' },
      grid: { vertLines: { color: '#f3f4f6' }, horzLines: { color: '#f3f4f6' } },
    });
    rDistChart.current = chart;
    const ro = new ResizeObserver(([e]) => chart.applyOptions({ width: e.contentRect.width }));
    ro.observe(rDistRef.current);
    return () => { ro.disconnect(); chart.remove(); rDistChart.current = null; };
  }, []);

  useEffect(() => {
    if (!rDistChart.current || !rDist?.buckets?.length) return;
    const series = rDistChart.current.addSeries(HistogramSeries, {});
    series.setData(rDist.buckets.map((b, i) => ({
      time: (i + 1) as unknown as Time,
      value: b.count,
      color: b.r_min >= 0 ? 'rgba(22,163,74,0.6)' : 'rgba(220,38,38,0.6)',
    })));
    return () => { try { rDistChart.current?.removeSeries(series); } catch {} };
  }, [rDist]);

  const m = metrics ?? {} as any;

  return (
    <div className="space-y-4">
      {/* Key stats */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-6 gap-3">
        <StatCard label="Best Trade" value={formatUSD(m.best_trade ?? 0)} color="text-green-600" />
        <StatCard label="Worst Trade" value={formatUSD(m.worst_trade ?? 0)} color="text-red-600" />
        <StatCard label="Avg Win" value={formatUSD(m.avg_win ?? 0)} color="text-green-600" />
        <StatCard label="Avg Loss" value={formatUSD(m.avg_loss ?? 0)} color="text-red-600" />
        <StatCard label="Win Streak" value={String(m.win_streak ?? 0)} />
        <StatCard label="Loss Streak" value={String(m.loss_streak ?? 0)} />
      </div>

      {/* Expectancy card */}
      {rDist && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Expectancy Breakdown</p>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
            <div>
              <p className="text-xs text-gray-400">Expectancy</p>
              <p className={cn('text-lg font-mono font-semibold', rDist.expectancy >= 0 ? 'text-green-600' : 'text-red-600')}>
                {rDist.expectancy.toFixed(3)}R
              </p>
            </div>
            <div>
              <p className="text-xs text-gray-400">Median R</p>
              <p className="text-lg font-mono font-semibold text-gray-900">{rDist.median_r.toFixed(3)}R</p>
            </div>
            <div>
              <p className="text-xs text-gray-400">Max Win</p>
              <p className="text-lg font-mono font-semibold text-green-600">{rDist.max_win_r.toFixed(2)}R</p>
            </div>
            <div>
              <p className="text-xs text-gray-400">Max Loss</p>
              <p className="text-lg font-mono font-semibold text-red-600">{rDist.max_loss_r.toFixed(2)}R</p>
            </div>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            E = (WR x AvgWin) - (LR x AvgLoss) = {rDist.expectancy.toFixed(3)}R per trade
          </p>
        </div>
      )}

      {/* PnL Distribution */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">PnL Distribution</p>
        <div ref={pnlRef} className="w-full" />
        {distData && (
          <div className="flex gap-4 text-xs text-gray-500 mt-2">
            <span>Mean: {formatUSD(distData.mean ?? 0)}</span>
            <span>Median: {formatUSD(distData.median ?? 0)}</span>
            <span>Std: {formatUSD(distData.std ?? 0)}</span>
          </div>
        )}
        {(!distData?.buckets || distData.buckets.length === 0) && (
          <p className="text-sm text-gray-400 text-center py-8">No trade data yet</p>
        )}
      </div>

      {/* R-multiple Distribution */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">R-Multiple Distribution</p>
        <div ref={rDistRef} className="w-full" />
        {(!rDist?.buckets || rDist.buckets.length === 0) && (
          <p className="text-sm text-gray-400 text-center py-8">No R-multiple data yet</p>
        )}
      </div>

      {/* Duration Analysis */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Duration vs Outcome</p>
        {durData?.buckets?.length ? (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  <th className="pb-2 font-medium">Duration</th>
                  <th className="pb-2 font-medium text-right">Trades</th>
                  <th className="pb-2 font-medium text-right">Win Rate</th>
                  <th className="pb-2 font-medium text-right">Avg R</th>
                </tr>
              </thead>
              <tbody>
                {durData.buckets.map((b, i) => (
                  <tr key={i} className="border-t border-gray-100">
                    <td className="py-2 text-gray-700">
                      {formatDuration(b.duration_min_s)} - {formatDuration(b.duration_max_s)}
                    </td>
                    <td className="py-2 text-right text-gray-700">{b.count}</td>
                    <td className={cn('py-2 text-right', b.win_rate >= 50 ? 'text-green-600' : 'text-red-600')}>
                      {b.win_rate.toFixed(1)}%
                    </td>
                    <td className={cn('py-2 text-right font-mono', b.avg_r >= 0 ? 'text-green-600' : 'text-red-600')}>
                      {b.avg_r.toFixed(2)}R
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-sm text-gray-400 text-center py-8">No duration data yet</p>
        )}
      </div>
    </div>
  );
}
