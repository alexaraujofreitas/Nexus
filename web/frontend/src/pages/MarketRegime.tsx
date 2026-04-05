import { useEffect, useRef, useState, useMemo, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { TrendingUp, RefreshCw } from 'lucide-react';
import { getPipelineStatus } from '../api/scanner';
import type { PipelineRow } from '../api/scanner';
import { cn } from '../lib/utils';

const MAX_SNAPSHOTS = 20;

// ── Regime visual config ─────────────────────────────────
const REGIME_CONFIG: Record<string, { label: string; bg: string; text: string; dot: string }> = {
  bull_trend:       { label: 'Bull',       bg: 'bg-green-100',  text: 'text-green-700',  dot: 'bg-green-500' },
  bear_trend:       { label: 'Bear',       bg: 'bg-red-100',    text: 'text-red-700',    dot: 'bg-red-500' },
  ranging:          { label: 'Range',      bg: 'bg-yellow-100', text: 'text-yellow-700', dot: 'bg-yellow-500' },
  vol_expansion:    { label: 'Vol+',       bg: 'bg-purple-100', text: 'text-purple-700', dot: 'bg-purple-500' },
  vol_compression:  { label: 'Vol-',       bg: 'bg-violet-100', text: 'text-violet-700', dot: 'bg-violet-500' },
  accumulation:     { label: 'Accum',      bg: 'bg-emerald-100',text: 'text-emerald-700',dot: 'bg-emerald-500' },
  distribution:     { label: 'Dist',       bg: 'bg-orange-100', text: 'text-orange-700', dot: 'bg-orange-500' },
  uncertain:        { label: 'Uncertain',  bg: 'bg-gray-100',   text: 'text-gray-500',   dot: 'bg-gray-400' },
  volatility_expansion: { label: 'Vol+',   bg: 'bg-purple-100', text: 'text-purple-700', dot: 'bg-purple-500' },
};

interface RegimeSnapshot {
  timestamp: string;        // ISO string of scan time
  displayTime: string;      // formatted for column header
  regimes: Record<string, string>; // symbol → regime
}

function formatScanTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
    + '\n' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function RegimeCell({ regime }: { regime: string | undefined }) {
  if (!regime) return <td className="px-2 py-2.5 text-center text-gray-200 text-xs">—</td>;
  const cfg = REGIME_CONFIG[regime] || REGIME_CONFIG['uncertain'];
  return (
    <td className="px-1.5 py-2 text-center">
      <span className={cn('inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide', cfg.bg, cfg.text)}>
        {cfg.label}
      </span>
    </td>
  );
}

export default function MarketRegime() {
  const snapshotsRef = useRef<RegimeSnapshot[]>([]);
  const [snapshots, setSnapshots] = useState<RegimeSnapshot[]>([]);
  const [refreshing, setRefreshing] = useState(false);

  // Poll pipeline status every 30s
  const { data: pipelineData, refetch } = useQuery({
    queryKey: ['pipeline-status-regime'],
    queryFn: getPipelineStatus,
    refetchInterval: 30000,
    staleTime: 15000,
  });

  // When pipeline data arrives, extract regime snapshot
  useEffect(() => {
    if (!pipelineData?.pipeline?.length) return;

    const pipeline: PipelineRow[] = pipelineData.pipeline;

    // Find the scan timestamp (use the first scanned symbol's timestamp)
    const scannedRows = pipeline.filter(r => r.scanned_at);
    if (scannedRows.length === 0) return;

    const scanTimestamp = scannedRows[0].scanned_at;
    if (!scanTimestamp) return;

    // Check if we already have this timestamp
    const existing = snapshotsRef.current;
    if (existing.length > 0 && existing[0].timestamp === scanTimestamp) return;

    // Build regime map for this scan cycle
    const regimes: Record<string, string> = {};
    for (const row of pipeline) {
      if (row.regime) {
        regimes[row.symbol] = row.regime;
      }
    }

    // Create snapshot
    const snapshot: RegimeSnapshot = {
      timestamp: scanTimestamp,
      displayTime: formatScanTime(scanTimestamp),
      regimes,
    };

    // Prepend (newest first), cap at MAX_SNAPSHOTS
    const updated = [snapshot, ...existing].slice(0, MAX_SNAPSHOTS);
    snapshotsRef.current = updated;
    setSnapshots(updated);
  }, [pipelineData]);

  // Collect all unique symbols across all snapshots, sorted
  const symbols = useMemo(() => {
    const set = new Set<string>();
    for (const snap of snapshots) {
      for (const sym of Object.keys(snap.regimes)) set.add(sym);
    }
    // Also add pipeline symbols that haven't been scanned yet
    if (pipelineData?.pipeline) {
      for (const row of pipelineData.pipeline) set.add(row.symbol);
    }
    return Array.from(set).sort();
  }, [snapshots, pipelineData]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try { await refetch(); } finally { setTimeout(() => setRefreshing(false), 500); }
  }, [refetch]);

  // Summary: count current regimes
  const currentRegimeCounts = useMemo(() => {
    if (snapshots.length === 0) return {};
    const counts: Record<string, number> = {};
    for (const [, regime] of Object.entries(snapshots[0].regimes)) {
      counts[regime] = (counts[regime] || 0) + 1;
    }
    return counts;
  }, [snapshots]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <TrendingUp className="w-5 h-5 text-blue-500" />
          <div>
            <h1 className="text-xl font-bold text-gray-900">Market Regime</h1>
            <p className="text-sm text-gray-500 mt-0.5">
              Rolling regime history by tradable pair ({snapshots.length} scan{snapshots.length !== 1 ? 's' : ''} captured)
            </p>
          </div>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold transition-colors',
            refreshing ? 'bg-gray-100 text-gray-400' : 'bg-blue-600 text-white hover:bg-blue-700',
          )}
        >
          <RefreshCw className={cn('w-4 h-4', refreshing && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {/* Current Regime Summary */}
      {Object.keys(currentRegimeCounts).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(currentRegimeCounts)
            .sort(([, a], [, b]) => b - a)
            .map(([regime, count]) => {
              const cfg = REGIME_CONFIG[regime] || REGIME_CONFIG['uncertain'];
              return (
                <div key={regime} className={cn('flex items-center gap-2 px-3 py-1.5 rounded-lg border', cfg.bg)}>
                  <span className={cn('w-2 h-2 rounded-full', cfg.dot)} />
                  <span className={cn('text-xs font-bold', cfg.text)}>
                    {(REGIME_CONFIG[regime]?.label || regime).toUpperCase()}
                  </span>
                  <span className={cn('text-xs font-mono', cfg.text)}>{count}</span>
                </div>
              );
            })}
        </div>
      )}

      {/* Regime Matrix Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {symbols.length === 0 || snapshots.length === 0 ? (
          <div className="p-12 text-center">
            <TrendingUp className="w-10 h-10 text-gray-200 mx-auto mb-3" />
            <p className="text-sm font-medium text-gray-500">Waiting for scan data...</p>
            <p className="text-xs text-gray-400 mt-1">Regime history will populate after the first scan cycle completes</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="sticky left-0 z-10 bg-gray-50 px-4 py-3 text-left text-[11px] font-bold text-gray-900 uppercase tracking-wider border-r border-gray-200 min-w-[130px]">
                    Tradable Pair
                  </th>
                  {snapshots.map((snap, i) => (
                    <th
                      key={snap.timestamp}
                      className={cn(
                        'px-2 py-2 text-center text-[10px] font-semibold uppercase tracking-wider min-w-[80px]',
                        i === 0 ? 'text-blue-700 bg-blue-50/50' : 'text-gray-500',
                      )}
                    >
                      <div className="whitespace-pre-line leading-tight">
                        {snap.displayTime}
                      </div>
                      {i === 0 && (
                        <span className="inline-block mt-0.5 px-1 py-0 rounded text-[8px] font-bold bg-blue-100 text-blue-600">
                          LATEST
                        </span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {symbols.map((sym, rowIdx) => (
                  <tr key={sym} className={cn('border-b border-gray-50', rowIdx % 2 === 0 ? '' : 'bg-gray-50/30')}>
                    <td className="sticky left-0 z-10 bg-white px-4 py-2.5 font-semibold text-gray-900 text-xs border-r border-gray-100 whitespace-nowrap">
                      {sym}
                    </td>
                    {snapshots.map((snap, colIdx) => (
                      <RegimeCell key={snap.timestamp} regime={snap.regimes[sym]} />
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-[11px] font-bold text-gray-900 uppercase tracking-wider mb-3">Regime Legend</p>
        <div className="flex flex-wrap gap-3">
          {Object.entries(REGIME_CONFIG)
            .filter(([key]) => !['volatility_expansion'].includes(key))
            .map(([key, cfg]) => (
              <div key={key} className="flex items-center gap-1.5">
                <span className={cn('w-2.5 h-2.5 rounded-full', cfg.dot)} />
                <span className={cn('px-1.5 py-0.5 rounded text-[10px] font-bold uppercase', cfg.bg, cfg.text)}>
                  {cfg.label}
                </span>
              </div>
            ))}
        </div>
      </div>
    </div>
  );
}
