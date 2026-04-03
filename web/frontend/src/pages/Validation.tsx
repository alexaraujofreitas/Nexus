import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldCheck, RefreshCw, CheckCircle, XCircle, AlertCircle } from 'lucide-react';
import { getValidationHealth, getReadiness, getDataIntegrity } from '../api/validation';
import { cn } from '../lib/utils';

const VERDICT_COLORS: Record<string, string> = {
  STILL_LEARNING: 'bg-gray-100 text-gray-700',
  IMPROVING: 'bg-yellow-100 text-yellow-700',
  READY_FOR_CAUTIOUS_LIVE: 'bg-green-100 text-green-700',
};

const STATUS_ICONS: Record<string, typeof CheckCircle> = {
  ok: CheckCircle,
  warning: AlertCircle,
  error: XCircle,
};

export default function Validation() {
  const queryClient = useQueryClient();
  const [refreshing, setRefreshing] = useState(false);

  const { data: healthData } = useQuery({ queryKey: ['val-health'], queryFn: getValidationHealth, refetchInterval: 30000 });
  const { data: readinessData } = useQuery({ queryKey: ['val-readiness'], queryFn: getReadiness, refetchInterval: 60000 });
  const { data: integrityData } = useQuery({ queryKey: ['val-integrity'], queryFn: getDataIntegrity, refetchInterval: 60000 });

  const handleRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['val-health'] }),
      queryClient.invalidateQueries({ queryKey: ['val-readiness'] }),
      queryClient.invalidateQueries({ queryKey: ['val-integrity'] }),
    ]);
    setTimeout(() => setRefreshing(false), 1000);
  };

  const components = healthData?.components || {};
  const checks = readinessData?.checks || [];
  const intChecks = integrityData?.checks || [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <ShieldCheck className="w-5 h-5 text-gray-400" />
          <h1 className="text-xl font-semibold text-gray-900">System Validation</h1>
        </div>
        <button onClick={handleRefresh} disabled={refreshing} className={cn(
          'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
          refreshing ? 'bg-gray-100 text-gray-400' : 'bg-blue-600 text-white hover:bg-blue-700',
        )}>
          <RefreshCw className={cn('w-4 h-4', refreshing && 'animate-spin')} />
          Run Checks
        </button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Health Overview */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Component Health</p>
          {Object.keys(components).length === 0 ? (
            <p className="text-sm text-gray-400">No health data available</p>
          ) : (
            <div className="space-y-2">
              {Object.entries(components).map(([name, comp]) => {
                const Icon = STATUS_ICONS[comp.status] || AlertCircle;
                const color = comp.status === 'ok' ? 'text-green-600' : comp.status === 'warning' ? 'text-amber-600' : 'text-red-600';
                return (
                  <div key={name} className="flex items-center justify-between py-2 border-b border-gray-50 last:border-0">
                    <div className="flex items-center gap-2">
                      <Icon className={cn('w-4 h-4', color)} />
                      <span className="text-sm font-medium text-gray-900 capitalize">{name}</span>
                    </div>
                    <div className="text-right">
                      <span className={cn('text-sm', color)}>{comp.status}</span>
                      {comp.detail && <p className="text-xs text-gray-400">{comp.detail}</p>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          {healthData?.thread_count !== undefined && (
            <div className="pt-2 border-t border-gray-100 text-sm text-gray-500">
              Threads: <span className={cn('font-mono', (healthData.thread_count ?? 0) > 75 ? 'text-red-600' : 'text-gray-700')}>{healthData.thread_count}</span>
              {healthData.uptime_s !== undefined && (
                <span className="ml-4">Uptime: {(healthData.uptime_s / 3600).toFixed(1)}h</span>
              )}
            </div>
          )}
        </div>

        {/* Readiness */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">System Readiness</p>
          {readinessData ? (
            <>
              <div className="flex items-center gap-3">
                <span className={cn('px-3 py-1.5 rounded-lg text-sm font-bold', VERDICT_COLORS[readinessData.verdict] || 'bg-gray-100 text-gray-700')}>
                  {readinessData.verdict?.replace(/_/g, ' ')}
                </span>
                <div className="flex-1">
                  <div className="w-full bg-gray-100 rounded-full h-2.5">
                    <div className="bg-blue-500 h-2.5 rounded-full transition-all" style={{ width: `${readinessData.score}%` }} />
                  </div>
                </div>
                <span className="text-sm font-mono text-gray-700">{readinessData.score}%</span>
              </div>
              <div className="space-y-1.5 max-h-60 overflow-y-auto">
                {checks.map((c, i) => (
                  <div key={i} className="flex items-center gap-2 text-sm">
                    {c.passed ? <CheckCircle className="w-3.5 h-3.5 text-green-500 shrink-0" /> : <XCircle className="w-3.5 h-3.5 text-red-500 shrink-0" />}
                    <span className="text-gray-700 flex-1">{c.name}</span>
                    <span className="text-xs text-gray-400 font-mono">{String(c.value)}/{String(c.threshold)}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <p className="text-sm text-gray-400">No readiness data available</p>
          )}
        </div>
      </div>

      {/* Data Integrity */}
      <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
        <div className="flex items-center justify-between">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Data Integrity</p>
          {integrityData && (
            <span className={cn('px-2 py-0.5 rounded text-xs font-medium', integrityData.passed ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700')}>
              {integrityData.passed ? 'ALL PASS' : 'ISSUES FOUND'}
            </span>
          )}
        </div>
        {intChecks.length === 0 ? (
          <p className="text-sm text-gray-400">No integrity checks available</p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
            {intChecks.map((c, i) => {
              const passed = c.status === 'pass' || c.status === 'PASS';
              return (
                <div key={i} className={cn('rounded-lg border p-3', passed ? 'border-green-200 bg-green-50/50' : 'border-red-200 bg-red-50/50')}>
                  <div className="flex items-center gap-2 mb-1">
                    {passed ? <CheckCircle className="w-3.5 h-3.5 text-green-600" /> : <XCircle className="w-3.5 h-3.5 text-red-600" />}
                    <span className="text-sm font-medium text-gray-900">{c.name}</span>
                  </div>
                  <p className="text-xs text-gray-500 ml-5">{c.detail}</p>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
