import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Activity, RefreshCw, ChevronDown, ChevronRight,
  CheckCircle2, XCircle, AlertTriangle, Clock, Filter as FilterIcon, Minus,
} from 'lucide-react';
import { getPipelineStatus, triggerScan } from '../api/scanner';
import type { PipelineRow, PipelineStatus, PipelineDiagnostics, MILBreakdown } from '../api/scanner';
import { useWSStore } from '../stores/wsStore';
import { cn, timeAgo, formatUSD } from '../lib/utils';

// ── Status configuration ──────────────────────────────────────
const STATUS_CONFIG: Record<PipelineStatus, { color: string; icon: typeof CheckCircle2; label: string }> = {
  Eligible:         { color: 'bg-green-100 text-green-700', icon: CheckCircle2,  label: 'Eligible' },
  'Risk Blocked':   { color: 'bg-red-100 text-red-700',    icon: XCircle,       label: 'Risk Blocked' },
  'No Signal':      { color: 'bg-gray-100 text-gray-600',  icon: Minus,         label: 'No Signal' },
  'Regime Filtered':{ color: 'bg-orange-100 text-orange-700', icon: AlertTriangle, label: 'Regime Filtered' },
  'Pre-Filter':     { color: 'bg-yellow-100 text-yellow-700', icon: FilterIcon,  label: 'Pre-Filter' },
  Waiting:          { color: 'bg-blue-100 text-blue-600',  icon: Clock,         label: 'Waiting' },
  Error:            { color: 'bg-red-50 text-red-500',     icon: AlertTriangle, label: 'Error' },
};

const REGIME_COLORS: Record<string, string> = {
  bull_trend:  'bg-green-100 text-green-700',
  bear_trend:  'bg-red-100 text-red-700',
  ranging:     'bg-yellow-100 text-yellow-700',
  uncertain:   'bg-gray-100 text-gray-600',
  vol_expansion: 'bg-purple-100 text-purple-700',
};

const ALL_STATUSES: PipelineStatus[] = [
  'Eligible', 'Risk Blocked', 'No Signal', 'Regime Filtered', 'Pre-Filter', 'Waiting', 'Error',
];

// ── Summary Card ──────────────────────────────────────────────
function SummaryCard({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div className={cn('rounded-lg border px-4 py-3 min-w-[140px]', color)}>
      <p className="text-xs font-medium opacity-70">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
    </div>
  );
}

// ── Diagnostic Expansion Panel ────────────────────────────────
function DiagnosticPanel({ row }: { row: PipelineRow }) {
  const d = row.diagnostics as PipelineDiagnostics;
  if (!d || !d.candle_count) {
    return (
      <div className="px-6 py-4 bg-gray-50 border-t border-gray-100 text-sm text-gray-500">
        No diagnostic data available (asset not yet scanned).
      </div>
    );
  }

  const stages = [
    {
      name: 'Data Fetch',
      passed: d.candle_count > 0,
      detail: d.candle_count > 0
        ? `${d.candle_count} bars loaded, latest: ${d.candle_ts_str || 'N/A'}, age: ${d.candle_age_s}s`
        : 'No data fetched',
    },
    {
      name: 'Indicators',
      passed: !d.indicator_cols_missing?.length,
      detail: d.indicator_cols_missing?.length
        ? `Missing: ${d.indicator_cols_missing.join(', ')}`
        : 'All indicators computed',
    },
    {
      name: 'Pre-Filter',
      passed: !d.pre_filter_reason,
      detail: d.pre_filter_reason || 'Passed',
    },
    {
      name: 'Regime',
      passed: !!row.regime,
      detail: row.regime
        ? `${row.regime.replace('_', ' ')} (confidence: ${(d.regime_confidence * 100).toFixed(0)}%)`
        : 'No regime classified',
    },
    {
      name: 'Strategy',
      passed: (row.models_fired?.length ?? 0) > 0,
      detail: row.models_fired?.length
        ? `Fired: ${row.models_fired.join(', ')}`
        : `No signal from: ${(d.models_no_signal || d.all_model_names || []).join(', ') || 'all models'}`,
    },
    {
      name: 'Confluence',
      passed: row.score > 0,
      detail: row.score > 0
        ? `Score: ${row.score.toFixed(3)}${row.technical_score > 0 ? ` (tech: ${row.technical_score.toFixed(3)})` : ''} | Direction: ${row.direction || 'N/A'}`
        : 'Below threshold or no signals',
    },
    {
      name: 'Risk Gate',
      passed: row.is_approved,
      detail: row.is_approved
        ? `Approved | R:R ${row.rr_ratio?.toFixed(1)} | Size ${formatUSD(row.position_size_usdt)}`
        : d.rejection_reason || row.reason || 'Not reached',
    },
  ];

  return (
    <div className="px-6 py-4 bg-gray-50 border-t border-gray-100">
      <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-3">
        Pipeline Diagnostics
      </h4>
      <div className="space-y-2">
        {stages.map((s) => (
          <div key={s.name} className="flex items-start gap-3 text-sm">
            <div className="mt-0.5">
              {s.passed ? (
                <CheckCircle2 className="w-4 h-4 text-green-500" />
              ) : (
                <XCircle className="w-4 h-4 text-red-400" />
              )}
            </div>
            <div>
              <span className="font-medium text-gray-700">{s.name}:</span>{' '}
              <span className="text-gray-600">{s.detail}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Signal Details */}
      {d.signal_details && Object.keys(d.signal_details).length > 0 && (
        <div className="mt-4">
          <h5 className="text-xs font-semibold text-gray-500 uppercase mb-2">Signal Details</h5>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            {Object.entries(d.signal_details).map(([model, info]) => (
              <div key={model} className="bg-white rounded border border-gray-200 px-3 py-2 text-xs">
                <span className="font-medium text-gray-700">{model}</span>
                <div className="mt-1 text-gray-500">
                  {typeof info === 'object' && info
                    ? `${(info as {direction?: string}).direction || '?'} | str: ${((info as {strength?: number}).strength ?? 0).toFixed(2)}`
                    : String(info)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Regime Probabilities */}
      {d.regime_probs && Object.keys(d.regime_probs).length > 0 && (
        <div className="mt-4">
          <h5 className="text-xs font-semibold text-gray-500 uppercase mb-2">Regime Probabilities</h5>
          <div className="flex gap-3 flex-wrap">
            {Object.entries(d.regime_probs).map(([regime, prob]) => (
              <div key={regime} className="text-xs">
                <span className="text-gray-600">{regime.replace('_', ' ')}:</span>{' '}
                <span className="font-mono font-medium">{((prob as number) * 100).toFixed(0)}%</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Disabled Models */}
      {d.models_disabled && d.models_disabled.length > 0 && (
        <div className="mt-3 text-xs text-gray-400">
          Disabled models: {d.models_disabled.join(', ')}
        </div>
      )}

      {/* MIL Intelligence */}
      <div className="mt-4">
        <h5 className="text-xs font-semibold text-gray-500 uppercase mb-2">Market Intelligence Layer</h5>
        {row.mil_active ? (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-3 text-xs">
              <span className="px-2 py-0.5 bg-green-50 text-green-600 rounded font-medium">MIL Active</span>
              <span className="text-gray-600">
                Influence: <span className="font-mono font-medium">{((row.mil_influence_pct || 0) * 100).toFixed(1)}%</span>
              </span>
              <span className="text-gray-600">
                Delta: <span className="font-mono font-medium">{(row.mil_total_delta || 0).toFixed(4)}</span>
              </span>
              {row.mil_capped && (
                <span className="px-2 py-0.5 bg-orange-50 text-orange-600 rounded font-medium">Capped</span>
              )}
              {row.mil_dominant_source && row.mil_dominant_source !== 'none' && (
                <span className="text-gray-600">
                  Dominant: <span className="font-medium">{row.mil_dominant_source}</span>
                </span>
              )}
            </div>
            {/* MIL Breakdown — all sources; sentiment/news hidden only when zero (placeholders) */}
            {row.mil_breakdown && Object.keys(row.mil_breakdown).length > 0 && (
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 mt-1">
                {Object.entries(row.mil_breakdown as MILBreakdown)
                  .filter(([key, val]) => {
                    // Hide sentiment/news placeholders only when zero (Phase 4B will populate)
                    if ((key === 'sentiment_delta' || key === 'news_delta') && Math.abs(val as number) < 0.0001) return false;
                    return true;
                  })
                  .map(([key, val]) => (
                  <div key={key} className="bg-white rounded border border-gray-200 px-2 py-1 text-xs">
                    <span className="text-gray-500 block truncate">{key.replace('_delta', '').replace('_', ' ')}</span>
                    <span className={cn(
                      'font-mono font-medium',
                      (val as number) > 0.001 ? 'text-green-600' : (val as number) < -0.001 ? 'text-red-600' : 'text-gray-400',
                    )}>
                      {(val as number) > 0 ? '+' : ''}{(val as number).toFixed(4)}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        ) : (
          <span className="text-xs text-gray-400">MIL disabled — pure technical scoring</span>
        )}
      </div>

      {/* Decision Explanation */}
      {row.decision_explanation && (
        <div className="mt-4 p-3 bg-blue-50 rounded border border-blue-200">
          <h5 className="text-xs font-semibold text-blue-700 uppercase mb-1">Decision Path</h5>
          <p className="text-xs text-blue-900">{row.decision_explanation}</p>
          {row.block_reasons && row.block_reasons.length > 0 && (
            <div className="mt-2 space-y-1">
              {row.block_reasons.map((reason, i) => (
                <div key={i} className="flex items-start gap-1.5 text-xs text-red-700">
                  <XCircle className="w-3 h-3 mt-0.5 flex-shrink-0" />
                  <span>{reason}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Trade details for eligible rows */}
      {row.is_approved && (
        <div className="mt-4 p-3 bg-green-50 rounded border border-green-200">
          <h5 className="text-xs font-semibold text-green-700 uppercase mb-2">Trade Details</h5>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
            <div>
              <span className="text-green-600">Entry</span>
              <p className="font-mono font-medium">{row.entry_price?.toFixed(2) ?? '—'}</p>
            </div>
            <div>
              <span className="text-red-600">Stop Loss</span>
              <p className="font-mono font-medium">{row.stop_loss?.toFixed(2) ?? '—'}</p>
            </div>
            <div>
              <span className="text-green-600">Take Profit</span>
              <p className="font-mono font-medium">{row.take_profit?.toFixed(2) ?? '—'}</p>
            </div>
            <div>
              <span className="text-gray-600">Position Size</span>
              <p className="font-mono font-medium">{formatUSD(row.position_size_usdt)}</p>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

// ── Pipeline Row ──────────────────────────────────────────────
function PipelineTableRow({ row, expanded, onToggle }: {
  row: PipelineRow;
  expanded: boolean;
  onToggle: () => void;
}) {
  const statusCfg = STATUS_CONFIG[row.status] || STATUS_CONFIG.Waiting;
  const StatusIcon = statusCfg.icon;
  const regimeColor = REGIME_COLORS[row.regime] || 'bg-gray-100 text-gray-500';

  return (
    <>
      <tr
        className={cn(
          'hover:bg-gray-50 cursor-pointer transition-colors border-b border-gray-100',
          expanded && 'bg-gray-50',
        )}
        onClick={onToggle}
      >
        {/* Expand chevron */}
        <td className="px-3 py-3 w-8">
          {expanded ? (
            <ChevronDown className="w-4 h-4 text-gray-400" />
          ) : (
            <ChevronRight className="w-4 h-4 text-gray-400" />
          )}
        </td>

        {/* Symbol */}
        <td className="px-3 py-3 font-semibold text-gray-900 whitespace-nowrap">
          {row.symbol}
        </td>

        {/* Price */}
        <td className="px-3 py-3 font-mono text-sm text-gray-700 whitespace-nowrap">
          {row.price != null ? formatUSD(row.price) : '—'}
        </td>

        {/* Regime */}
        <td className="px-3 py-3">
          {row.regime ? (
            <span className={cn('px-2 py-0.5 rounded text-xs font-medium', regimeColor)}>
              {row.regime.replace('_', ' ')}
            </span>
          ) : (
            <span className="text-xs text-gray-400">—</span>
          )}
        </td>

        {/* Weight */}
        <td className="px-3 py-3 font-mono text-sm text-gray-600">
          {row.allocation_weight.toFixed(1)}
        </td>

        {/* Strategy */}
        <td className="px-3 py-3">
          <div className="flex flex-wrap gap-1">
            {row.models_fired?.length ? (
              row.models_fired.map((m) => (
                <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">
                  {m}
                </span>
              ))
            ) : (
              <span className="text-xs text-gray-400">—</span>
            )}
          </div>
        </td>

        {/* Score */}
        <td className="px-3 py-3">
          {row.score > 0 ? (
            <div className="flex items-center gap-2">
              <div className="w-16 bg-gray-100 rounded-full h-1.5">
                <div
                  className="bg-blue-500 h-1.5 rounded-full"
                  style={{ width: `${Math.min(row.score * 100, 100)}%` }}
                />
              </div>
              <span className="font-mono text-xs text-gray-700">{row.score.toFixed(2)}</span>
            </div>
          ) : (
            <span className="text-xs text-gray-400">—</span>
          )}
        </td>

        {/* Status */}
        <td className="px-3 py-3">
          <span className={cn('inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium', statusCfg.color)}>
            <StatusIcon className="w-3 h-3" />
            {statusCfg.label}
          </span>
        </td>

        {/* Reason */}
        <td className="px-3 py-3 text-xs text-gray-500 max-w-[200px] truncate" title={row.reason}>
          {row.reason || '—'}
        </td>

        {/* Scanned */}
        <td className="px-3 py-3 text-xs text-gray-400 whitespace-nowrap font-mono">
          {row.scanned_at
            ? new Date(row.scanned_at.endsWith('Z') ? row.scanned_at : row.scanned_at + 'Z')
                .toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
            : '—'}
        </td>
      </tr>

      {/* Expanded diagnostic row */}
      {expanded && (
        <tr>
          <td colSpan={10}>
            <DiagnosticPanel row={row} />
          </td>
        </tr>
      )}
    </>
  );
}

// ── Main Scanner Page ─────────────────────────────────────────
export default function Scanner() {
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();
  const [triggering, setTriggering] = useState(false);
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [statusFilter, setStatusFilter] = useState<PipelineStatus | 'All'>('All');
  const [regimeFilter, setRegimeFilter] = useState<string>('All');
  const [sortField, setSortField] = useState<'symbol' | 'score' | 'status'>('symbol');
  const [sortAsc, setSortAsc] = useState(true);

  // WS subscription
  useEffect(() => {
    if (status === 'connected') {
      subscribe('scanner');
    }
  }, [status, subscribe]);

  // When WS pushes scanner data, invalidate pipeline query
  useEffect(() => {
    const wsData = lastMessage['scanner'];
    if (wsData) {
      queryClient.invalidateQueries({ queryKey: ['pipeline-status'] });
    }
  }, [lastMessage, queryClient]);

  // API query — pipeline-status
  const { data: pipelineData, isLoading } = useQuery({
    queryKey: ['pipeline-status'],
    queryFn: getPipelineStatus,
    refetchInterval: 15000,
  });

  const pipeline = pipelineData?.pipeline || [];
  const summary = pipelineData?.summary || { total: 0, eligible: 0, active_signals: 0, blocked: 0 };
  const scannerRunning = pipelineData?.scanner_running ?? false;
  const lastScanAt = pipelineData?.last_scan_at || '';

  // Collect unique regimes for filter
  const uniqueRegimes = useMemo(() => {
    const set = new Set<string>();
    pipeline.forEach((r) => { if (r.regime) set.add(r.regime); });
    return Array.from(set).sort();
  }, [pipeline]);

  // Filter + sort
  const filteredRows = useMemo(() => {
    let rows = pipeline;
    if (statusFilter !== 'All') {
      rows = rows.filter((r) => r.status === statusFilter);
    }
    if (regimeFilter !== 'All') {
      rows = rows.filter((r) => r.regime === regimeFilter);
    }
    // Sort
    rows = [...rows].sort((a, b) => {
      let cmp = 0;
      if (sortField === 'symbol') cmp = a.symbol.localeCompare(b.symbol);
      else if (sortField === 'score') cmp = a.score - b.score;
      else if (sortField === 'status') cmp = a.status.localeCompare(b.status);
      return sortAsc ? cmp : -cmp;
    });
    return rows;
  }, [pipeline, statusFilter, regimeFilter, sortField, sortAsc]);

  const toggleRow = (symbol: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(symbol)) next.delete(symbol);
      else next.add(symbol);
      return next;
    });
  };

  const handleSort = (field: typeof sortField) => {
    if (sortField === field) setSortAsc(!sortAsc);
    else { setSortField(field); setSortAsc(true); }
  };

  const handleTrigger = async () => {
    setTriggering(true);
    try {
      await triggerScan();
    } finally {
      setTimeout(() => setTriggering(false), 2000);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Activity className="w-5 h-5 text-blue-500" />
          <h1 className="text-xl font-semibold text-gray-900">Scan Pipeline</h1>
          <span
            className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              scannerRunning ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500',
            )}
          >
            {scannerRunning ? 'Running' : 'Stopped'}
          </span>
          {lastScanAt && (
            <span className="text-xs text-gray-400">Last scan: {
              new Date(lastScanAt.endsWith('Z') ? lastScanAt : lastScanAt + 'Z')
                .toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
            }</span>
          )}
        </div>
        <button
          onClick={handleTrigger}
          disabled={triggering}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium transition-colors min-h-[44px]',
            triggering
              ? 'bg-gray-100 text-gray-400 cursor-not-allowed'
              : 'bg-blue-600 text-white hover:bg-blue-700 active:bg-blue-800',
          )}
        >
          <RefreshCw className={cn('w-4 h-4', triggering && 'animate-spin')} />
          {triggering ? 'Scanning...' : 'Trigger Scan'}
        </button>
      </div>

      {/* Summary Cards */}
      <div className="flex flex-wrap gap-3">
        <SummaryCard label="Total in Universe" value={summary.total} color="border-gray-200 bg-white text-gray-900" />
        <SummaryCard label="Eligible Now" value={summary.eligible} color="border-green-200 bg-green-50 text-green-800" />
        <SummaryCard label="Active Signals" value={summary.active_signals} color="border-blue-200 bg-blue-50 text-blue-800" />
        <SummaryCard label="Blocked" value={summary.blocked} color="border-red-200 bg-red-50 text-red-800" />
      </div>

      {/* Filters */}
      <div className="flex flex-wrap gap-3 items-center">
        <div className="flex items-center gap-2">
          <FilterIcon className="w-4 h-4 text-gray-400" />
          <select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value as PipelineStatus | 'All')}
            className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 bg-white text-gray-700 min-h-[36px]"
          >
            <option value="All">All Statuses</option>
            {ALL_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </div>
        <select
          value={regimeFilter}
          onChange={(e) => setRegimeFilter(e.target.value)}
          className="text-sm border border-gray-200 rounded-lg px-3 py-1.5 bg-white text-gray-700 min-h-[36px]"
        >
          <option value="All">All Regimes</option>
          {uniqueRegimes.map((r) => (
            <option key={r} value={r}>{r.replace('_', ' ')}</option>
          ))}
        </select>
        <span className="text-xs text-gray-400 ml-auto">
          {filteredRows.length} of {pipeline.length} assets
        </span>
      </div>

      {/* Pipeline Table */}
      {isLoading ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <RefreshCw className="w-6 h-6 text-gray-300 mx-auto mb-2 animate-spin" />
          <p className="text-sm text-gray-400">Loading pipeline status...</p>
        </div>
      ) : filteredRows.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <Activity className="w-8 h-8 text-gray-300 mx-auto mb-2" />
          <p className="text-sm text-gray-400">
            {pipeline.length === 0
              ? 'No tradable assets configured. Enable assets in Asset Management.'
              : 'No assets match the current filters.'}
          </p>
        </div>
      ) : (
        <div className="bg-white rounded-lg border border-gray-200 overflow-hidden">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-3 py-2.5 w-8"></th>
                  <th
                    className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase cursor-pointer hover:text-gray-700"
                    onClick={() => handleSort('symbol')}
                  >
                    Symbol {sortField === 'symbol' && (sortAsc ? '↑' : '↓')}
                  </th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Price</th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Regime</th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Weight</th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Strategy</th>
                  <th
                    className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase cursor-pointer hover:text-gray-700"
                    onClick={() => handleSort('score')}
                  >
                    Score {sortField === 'score' && (sortAsc ? '↑' : '↓')}
                  </th>
                  <th
                    className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase cursor-pointer hover:text-gray-700"
                    onClick={() => handleSort('status')}
                  >
                    Status {sortField === 'status' && (sortAsc ? '↑' : '↓')}
                  </th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Reason</th>
                  <th className="px-3 py-2.5 text-left text-xs font-medium text-gray-500 uppercase">Scanned</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row) => (
                  <PipelineTableRow
                    key={row.symbol}
                    row={row}
                    expanded={expandedRows.has(row.symbol)}
                    onToggle={() => toggleRow(row.symbol)}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
