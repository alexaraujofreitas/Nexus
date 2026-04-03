import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { TrendingUp } from 'lucide-react';
import { getPerformanceMetrics } from '../api/analytics';
import { useWSStore } from '../stores/wsStore';
import { cn, formatPct } from '../lib/utils';
import EquityDrawdown from './analytics/EquityDrawdown';
import TradePerformance from './analytics/TradePerformance';
import ModelPerformance from './analytics/ModelPerformance';
import RegimeAnalysis from './analytics/RegimeAnalysis';

type TabKey = 'equity' | 'trades' | 'models' | 'regime';

const TABS: { key: TabKey; label: string }[] = [
  { key: 'equity', label: 'Equity & Drawdown' },
  { key: 'trades', label: 'Trade Performance' },
  { key: 'models', label: 'Models' },
  { key: 'regime', label: 'Regime Analysis' },
];

function MetricCard({ label, value, baseline, format = 'number' }: {
  label: string; value: number; baseline?: number; format?: 'number' | 'pct' | 'usd' | 'r';
}) {
  const fmt = format === 'usd'
    ? new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(value)
    : format === 'pct' ? formatPct(value)
    : format === 'r' ? `${value.toFixed(2)}R`
    : value.toFixed(2);
  const color = baseline !== undefined ? (value >= baseline ? 'text-green-600' : 'text-red-600') : 'text-gray-900';
  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      <p className="text-xs text-gray-400 mb-1">{label}</p>
      <p className={cn('text-xl font-mono font-semibold', color)}>{fmt}</p>
    </div>
  );
}

export default function Analytics() {
  const [searchParams, setSearchParams] = useSearchParams();
  const tabParam = searchParams.get('tab') as TabKey | null;
  const activeTab = TABS.some((t) => t.key === tabParam) ? tabParam! : 'equity';

  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => { if (status === 'connected') subscribe('trades'); }, [status, subscribe]);
  useEffect(() => {
    if (lastMessage['trades']) {
      queryClient.invalidateQueries({ queryKey: ['equity-curve'] });
      queryClient.invalidateQueries({ queryKey: ['perf-metrics'] });
      queryClient.invalidateQueries({ queryKey: ['drawdown-curve'] });
      queryClient.invalidateQueries({ queryKey: ['rolling-metrics'] });
      queryClient.invalidateQueries({ queryKey: ['r-distribution'] });
      queryClient.invalidateQueries({ queryKey: ['duration-analysis'] });
      queryClient.invalidateQueries({ queryKey: ['by-regime'] });
      queryClient.invalidateQueries({ queryKey: ['regime-transitions'] });
      queryClient.invalidateQueries({ queryKey: ['model-breakdown'] });
      queryClient.invalidateQueries({ queryKey: ['trade-dist'] });
    }
  }, [lastMessage, queryClient]);

  const { data: metrics } = useQuery({
    queryKey: ['perf-metrics'],
    queryFn: getPerformanceMetrics,
    refetchInterval: 30000,
  });

  const m = metrics ?? ({} as any);

  function setTab(key: TabKey) {
    setSearchParams({ tab: key });
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <TrendingUp className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Performance Analytics</h1>
      </div>

      {/* Summary cards (always visible) */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
        <MetricCard label="Total Trades" value={m.total_trades ?? 0} />
        <MetricCard label="Win Rate" value={m.win_rate ?? 0} baseline={45} format="pct" />
        <MetricCard label="Profit Factor" value={m.profit_factor ?? 0} baseline={1.1} />
        <MetricCard label="Avg R" value={m.avg_r ?? 0} baseline={0.1} format="r" />
        <MetricCard label="Max Drawdown" value={Math.abs(m.max_drawdown_pct ?? 0)} format="pct" />
      </div>

      {/* Tab bar */}
      <div className="border-b border-gray-200">
        <nav className="flex gap-0 overflow-x-auto" role="tablist">
          {TABS.map((tab) => (
            <button
              key={tab.key}
              role="tab"
              aria-selected={activeTab === tab.key}
              onClick={() => setTab(tab.key)}
              className={cn(
                'px-4 py-2 text-sm font-medium whitespace-nowrap border-b-2 transition-colors min-h-[44px]',
                activeTab === tab.key
                  ? 'border-blue-600 text-blue-600'
                  : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300'
              )}
            >
              {tab.label}
            </button>
          ))}
        </nav>
      </div>

      {/* Tab content */}
      <div>
        {activeTab === 'equity' && <EquityDrawdown />}
        {activeTab === 'trades' && <TradePerformance />}
        {activeTab === 'models' && <ModelPerformance />}
        {activeTab === 'regime' && <RegimeAnalysis />}
      </div>
    </div>
  );
}
