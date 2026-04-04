import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getPerformanceByRegime, getRegimeTransitions } from '../../api/analytics';
import type { RegimeTransition } from '../../api/analytics';
import { cn } from '../../lib/utils';

const REGIME_COLORS: Record<string, string> = {
  bull_trend: '#16a34a',
  bear_trend: '#dc2626',
  ranging: '#ca8a04',
  vol_expansion: '#7c3aed',
  uncertain: '#6b7280',
};

function regimeColor(name: string): string {
  return REGIME_COLORS[name] ?? '#6b7280';
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${(seconds / 3600).toFixed(1)}h`;
  return `${(seconds / 86400).toFixed(1)}d`;
}

export default function RegimeAnalysis() {
  const { data: regimeData } = useQuery({
    queryKey: ['by-regime'],
    queryFn: getPerformanceByRegime,
    refetchInterval: 60000,
  });

  const { data: transData } = useQuery({
    queryKey: ['regime-transitions'],
    queryFn: getRegimeTransitions,
    refetchInterval: 60000,
  });

  const regimes = regimeData?.regimes ?? [];
  const transitions = transData?.transitions ?? [];

  // Build the transition matrix for display
  const regimeNames = useMemo(() => {
    const names = new Set<string>();
    transitions.forEach((t) => {
      names.add(t.from);
      names.add(t.to);
    });
    return Array.from(names).sort();
  }, [transitions]);

  const transitionMap = useMemo(() => {
    const map: Record<string, RegimeTransition> = {};
    transitions.forEach((t) => { map[`${t.from}->${t.to}`] = t; });
    return map;
  }, [transitions]);

  const maxPF = useMemo(() => {
    return Math.max(...regimes.map((r) => r.pf), 1);
  }, [regimes]);

  return (
    <div className="space-y-4">
      {/* Regime Distribution (horizontal bar) */}
      {regimes.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Regime Distribution</p>
          <div className="flex h-8 rounded overflow-hidden">
            {regimes.map((r) => (
              <div
                key={r.name}
                style={{ width: `${r.pct_of_total}%`, backgroundColor: regimeColor(r.name) }}
                className="flex items-center justify-center min-w-[2px] transition-all"
                title={`${r.name}: ${r.pct_of_total.toFixed(1)}%`}
              >
                {r.pct_of_total >= 10 && (
                  <span className="text-xs text-white font-medium truncate px-1">
                    {r.pct_of_total.toFixed(0)}%
                  </span>
                )}
              </div>
            ))}
          </div>
          <div className="flex flex-wrap gap-3 mt-2">
            {regimes.map((r) => (
              <span key={r.name} className="flex items-center gap-1 text-xs text-gray-600">
                <span
                  className="inline-block w-2.5 h-2.5 rounded-sm"
                  style={{ backgroundColor: regimeColor(r.name) }}
                />
                {r.name.replace('_', ' ')} ({r.pct_of_total.toFixed(1)}%)
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Regime Performance Table */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Performance by Regime</p>
        {regimes.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-8">No regime data yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  <th className="pb-2 font-medium">Regime</th>
                  <th className="pb-2 font-medium text-right">Trades</th>
                  <th className="pb-2 font-medium text-right">% of Total</th>
                  <th className="pb-2 font-medium text-right">Win Rate</th>
                  <th className="pb-2 font-medium text-right">PF</th>
                  <th className="pb-2 font-medium text-right">Avg R</th>
                  <th className="pb-2 font-medium text-right">Avg Duration</th>
                </tr>
              </thead>
              <tbody>
                {regimes.map((r) => (
                  <tr key={r.name} className="border-t border-gray-100">
                    <td className="py-2">
                      <span className="flex items-center gap-2">
                        <span
                          className="inline-block w-2.5 h-2.5 rounded-sm flex-shrink-0"
                          style={{ backgroundColor: regimeColor(r.name) }}
                        />
                        <span className="font-medium text-gray-900">{r.name.replace('_', ' ')}</span>
                      </span>
                    </td>
                    <td className="py-2 text-right text-gray-700">{r.trades}</td>
                    <td className="py-2 text-right text-gray-700">{r.pct_of_total.toFixed(1)}%</td>
                    <td className={cn('py-2 text-right', r.win_rate >= 50 ? 'text-green-600' : 'text-red-600')}>
                      {r.win_rate.toFixed(1)}%
                    </td>
                    <td className={cn('py-2 text-right font-mono', r.pf >= 1.0 ? 'text-green-600' : 'text-red-600')}>
                      {r.pf.toFixed(2)}
                    </td>
                    <td className={cn('py-2 text-right font-mono', r.avg_r >= 0 ? 'text-green-600' : 'text-red-600')}>
                      {r.avg_r.toFixed(2)}R
                    </td>
                    <td className="py-2 text-right text-gray-700">{formatDuration(r.avg_duration_s)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* PF Comparison Bar Chart (CSS-based) */}
      {regimes.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Profit Factor by Regime</p>
          <div className="space-y-2">
            {regimes.map((r) => (
              <div key={r.name} className="flex items-center gap-3">
                <span className="text-xs text-gray-600 w-28 truncate">{r.name.replace('_', ' ')}</span>
                <div className="flex-1 bg-gray-100 rounded-full h-5 relative">
                  <div
                    className="h-5 rounded-full transition-all"
                    style={{
                      width: `${Math.min((r.pf / maxPF) * 100, 100)}%`,
                      backgroundColor: regimeColor(r.name),
                      opacity: 0.7,
                    }}
                  />
                  <span className="absolute inset-y-0 right-2 flex items-center text-xs font-mono text-gray-700">
                    {r.pf.toFixed(2)}
                  </span>
                </div>
              </div>
            ))}
          </div>
          {/* PF = 1.0 reference line note */}
          <p className="text-xs text-gray-400 mt-2">PF = 1.0 is breakeven; above = profitable</p>
        </div>
      )}

      {/* Regime Transition Matrix */}
      {regimeNames.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Regime Transition Matrix</p>
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr>
                  <th className="pb-2 text-left font-medium text-gray-500">From \ To</th>
                  {regimeNames.map((name) => (
                    <th key={name} className="pb-2 text-center font-medium text-gray-500 min-w-[80px]">
                      {name.replace('_', ' ')}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {regimeNames.map((from) => (
                  <tr key={from} className="border-t border-gray-100">
                    <td className="py-2 font-medium text-gray-700">{from.replace('_', ' ')}</td>
                    {regimeNames.map((to) => {
                      const t = transitionMap[`${from}->${to}`];
                      if (!t) {
                        return <td key={to} className="py-2 text-center text-gray-300">-</td>;
                      }
                      const intensity = Math.min(t.count / 20, 1);
                      const bg = t.avg_pnl_during_transition >= 0
                        ? `rgba(22,163,74,${0.1 + intensity * 0.4})`
                        : `rgba(220,38,38,${0.1 + intensity * 0.4})`;
                      return (
                        <td
                          key={to}
                          className="py-2 text-center font-mono"
                          style={{ backgroundColor: bg }}
                          title={`${t.count} transitions, avg PnL: ${t.avg_pnl_during_transition.toFixed(2)}`}
                        >
                          <div className="text-gray-900">{t.count}</div>
                          <div className={cn('text-[10px]', t.avg_pnl_during_transition >= 0 ? 'text-green-700' : 'text-red-700')}>
                            {t.avg_pnl_during_transition >= 0 ? '+' : ''}{t.avg_pnl_during_transition.toFixed(1)}
                          </div>
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-gray-400 mt-2">
            Cells show transition count and average PnL. Green = positive, red = negative.
          </p>
        </div>
      )}

      {transitions.length === 0 && regimes.length > 0 && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">Regime Transitions</p>
          <p className="text-sm text-gray-400 text-center py-8">No transition data yet</p>
        </div>
      )}
    </div>
  );
}
