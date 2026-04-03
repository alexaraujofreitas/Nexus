import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getModelBreakdown } from '../../api/analytics';
import type { ModelBreakdown } from '../../api/analytics';
import { cn } from '../../lib/utils';

type SortField = 'name' | 'trades' | 'win_rate' | 'pf' | 'avg_r';
type SortOrder = 'asc' | 'desc';

const REGIME_OPTIONS = ['All', 'bull_trend', 'bear_trend', 'ranging', 'vol_expansion'] as const;

function pfColor(pf: number): string {
  if (pf >= 1.2) return 'text-green-600';
  if (pf >= 1.0) return 'text-yellow-600';
  return 'text-red-600';
}

function pfBg(pf: number): string {
  if (pf >= 1.2) return 'bg-green-50';
  if (pf >= 1.0) return 'bg-yellow-50';
  return 'bg-red-50';
}

function SortArrow({ field, active, order }: { field: SortField; active: SortField; order: SortOrder }) {
  if (field !== active) return <span className="text-gray-300 ml-1">&#8597;</span>;
  return <span className="text-blue-600 ml-1">{order === 'asc' ? '\u25B2' : '\u25BC'}</span>;
}

export default function ModelPerformance() {
  const [sortField, setSortField] = useState<SortField>('pf');
  const [sortOrder, setSortOrder] = useState<SortOrder>('desc');
  const [regimeFilter, setRegimeFilter] = useState<string>('All');

  const queryParams = useMemo(() => {
    const params: Record<string, string> = { sort: sortField, order: sortOrder };
    if (regimeFilter !== 'All') params.regime = regimeFilter;
    return params;
  }, [sortField, sortOrder, regimeFilter]);

  const { data: modelData } = useQuery({
    queryKey: ['model-breakdown', queryParams],
    queryFn: () => getModelBreakdown(queryParams),
    refetchInterval: 60000,
  });

  const models = modelData?.models ?? [];

  const sorted = useMemo(() => {
    const copy = [...models];
    copy.sort((a, b) => {
      const aVal = a[sortField as keyof ModelBreakdown];
      const bVal = b[sortField as keyof ModelBreakdown];
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return sortOrder === 'asc' ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      const numA = Number(aVal) || 0;
      const numB = Number(bVal) || 0;
      return sortOrder === 'asc' ? numA - numB : numB - numA;
    });
    return copy;
  }, [models, sortField, sortOrder]);

  function handleSort(field: SortField) {
    if (field === sortField) {
      setSortOrder(sortOrder === 'asc' ? 'desc' : 'asc');
    } else {
      setSortField(field);
      setSortOrder('desc');
    }
  }

  return (
    <div className="space-y-4">
      {/* Filters */}
      <div className="flex flex-wrap gap-2 items-center">
        <span className="text-xs text-gray-500 font-medium">Regime:</span>
        {REGIME_OPTIONS.map((r) => (
          <button
            key={r}
            onClick={() => setRegimeFilter(r)}
            className={cn(
              'px-3 py-1 text-xs rounded-full font-medium transition-colors min-h-[44px]',
              regimeFilter === r
                ? 'bg-blue-600 text-white'
                : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
            )}
          >
            {r === 'All' ? 'All Regimes' : r.replace('_', ' ')}
          </button>
        ))}
      </div>

      {/* Model Table */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">
          Strategy / Model Breakdown
          {regimeFilter !== 'All' && (
            <span className="ml-2 text-blue-600 normal-case">({regimeFilter.replace('_', ' ')})</span>
          )}
        </p>

        {sorted.length === 0 ? (
          <p className="text-sm text-gray-400 text-center py-8">No model data yet</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500">
                  {([
                    ['name', 'Model'],
                    ['trades', 'Trades'],
                    ['win_rate', 'Win Rate'],
                    ['pf', 'PF'],
                    ['avg_r', 'Avg R'],
                  ] as [SortField, string][]).map(([field, label]) => (
                    <th
                      key={field}
                      className={cn(
                        'pb-2 font-medium cursor-pointer select-none hover:text-blue-600 transition-colors',
                        field !== 'name' && 'text-right'
                      )}
                      onClick={() => handleSort(field)}
                    >
                      {label}
                      <SortArrow field={field} active={sortField} order={sortOrder} />
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {sorted.map((mod) => (
                  <tr key={mod.name} className={cn('border-t border-gray-100', pfBg(mod.pf))}>
                    <td className="py-2 font-medium text-gray-900">
                      {mod.name}
                      {mod.active === false && (
                        <span className="ml-2 text-xs bg-gray-200 text-gray-500 rounded px-1">disabled</span>
                      )}
                    </td>
                    <td className="py-2 text-right text-gray-700">{mod.trades}</td>
                    <td className={cn('py-2 text-right', mod.win_rate >= 45 ? 'text-green-600' : 'text-red-600')}>
                      {mod.win_rate.toFixed(1)}%
                    </td>
                    <td className={cn('py-2 text-right font-mono', pfColor(mod.pf))}>
                      {mod.pf.toFixed(2)}
                    </td>
                    <td className={cn('py-2 text-right font-mono', mod.avg_r >= 0 ? 'text-green-600' : 'text-red-600')}>
                      {mod.avg_r.toFixed(2)}R
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Summary */}
      {sorted.length > 0 && (
        <div className="grid grid-cols-3 gap-3">
          <div className="bg-green-50 rounded-lg border border-green-200 p-3 text-center">
            <p className="text-xs text-green-700 mb-1">Profitable (PF &ge; 1.2)</p>
            <p className="text-lg font-mono font-semibold text-green-700">
              {sorted.filter((m) => m.pf >= 1.2).length}
            </p>
          </div>
          <div className="bg-yellow-50 rounded-lg border border-yellow-200 p-3 text-center">
            <p className="text-xs text-yellow-700 mb-1">Marginal (1.0 - 1.2)</p>
            <p className="text-lg font-mono font-semibold text-yellow-700">
              {sorted.filter((m) => m.pf >= 1.0 && m.pf < 1.2).length}
            </p>
          </div>
          <div className="bg-red-50 rounded-lg border border-red-200 p-3 text-center">
            <p className="text-xs text-red-700 mb-1">Unprofitable (PF &lt; 1.0)</p>
            <p className="text-lg font-mono font-semibold text-red-700">
              {sorted.filter((m) => m.pf < 1.0).length}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
