import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Search, RefreshCw, Crosshair } from 'lucide-react';
import { getScannerResults, getWatchlist, triggerScan } from '../api/scanner';
import type { OrderCandidate } from '../api/scanner';
import { useWSStore } from '../stores/wsStore';
import { cn, timeAgo, formatUSD } from '../lib/utils';

// ── Regime badge color map ──────────────────────────────────
const REGIME_COLORS: Record<string, string> = {
  bull_trend: 'bg-green-100 text-green-700',
  bear_trend: 'bg-red-100 text-red-700',
  ranging: 'bg-yellow-100 text-yellow-700',
  uncertain: 'bg-gray-100 text-gray-600',
};

// ── Symbol Card ─────────────────────────────────────────────
function SymbolCard({ c, flash }: { c: OrderCandidate; flash: boolean }) {
  return (
    <div
      className={cn(
        'bg-white rounded-lg border border-gray-200 p-4 transition-all duration-300',
        flash && 'ring-2 ring-green-400 ring-opacity-50',
      )}
    >
      {/* Row 1: Symbol + Regime */}
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-gray-900">{c.symbol}</span>
        <span
          className={cn(
            'px-2 py-0.5 rounded text-xs font-medium',
            REGIME_COLORS[c.regime] || 'bg-gray-100 text-gray-600',
          )}
        >
          {c.regime.replace('_', ' ')}
        </span>
      </div>

      {/* Row 2: Score bar + direction */}
      <div className="flex items-center gap-3 mb-3">
        <div className="flex-1">
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-gray-500">Score</span>
            <span className="font-mono text-gray-700">{c.score.toFixed(2)}</span>
          </div>
          <div className="w-full bg-gray-100 rounded-full h-2">
            <div
              className="bg-blue-500 h-2 rounded-full transition-all"
              style={{ width: `${Math.min(c.score * 100, 100)}%` }}
            />
          </div>
        </div>
        <span
          className={cn(
            'text-lg font-bold',
            c.direction === 'buy' || c.direction === 'long' ? 'text-green-600' : 'text-red-600',
          )}
        >
          {c.direction === 'buy' || c.direction === 'long' ? '▲ BUY' : '▼ SELL'}
        </span>
      </div>

      {/* Row 3: Models fired */}
      <div className="flex flex-wrap gap-1 mb-3">
        {(c.models_fired || []).map((m) => (
          <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">
            {m}
          </span>
        ))}
      </div>

      {/* Row 4: Entry / SL / TP */}
      <div className="grid grid-cols-3 gap-2 text-xs mb-2">
        <div>
          <span className="text-gray-400">Entry</span>
          <p className="font-mono text-gray-900">{c.entry_price?.toFixed(2) ?? '—'}</p>
        </div>
        <div>
          <span className="text-gray-400">SL</span>
          <p className="font-mono text-red-600">{c.stop_loss?.toFixed(2) ?? '—'}</p>
        </div>
        <div>
          <span className="text-gray-400">TP</span>
          <p className="font-mono text-green-600">{c.take_profit?.toFixed(2) ?? '—'}</p>
        </div>
      </div>

      {/* Row 5: R:R + Approval + Size */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-500">
          R:R <span className="font-mono text-gray-700">{c.rr_ratio?.toFixed(1) ?? '—'}</span>
        </span>
        <span
          className={cn(
            'font-medium',
            c.approved ? 'text-green-600' : 'text-red-500',
          )}
          title={c.rejection_reason || ''}
        >
          {c.approved ? '✓ Approved' : `✗ ${c.rejection_reason || 'Rejected'}`}
        </span>
        <span className="font-mono text-gray-700">
          {formatUSD(c.position_size_usdt ?? 0)}
        </span>
      </div>

      {/* Timestamp */}
      <p className="text-xs text-gray-400 mt-2">{timeAgo(c.generated_at)}</p>
    </div>
  );
}

// ── Scanner Page ────────────────────────────────────────────
export default function Scanner() {
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();
  const [triggering, setTriggering] = useState(false);
  const [flashIds, setFlashIds] = useState<Set<string>>(new Set());

  // WS subscription
  useEffect(() => {
    if (status === 'connected') {
      subscribe('scanner');
    }
  }, [status, subscribe]);

  // When WS pushes scanner data, update query cache
  useEffect(() => {
    const wsData = lastMessage['scanner'];
    if (wsData) {
      queryClient.setQueryData(['scanner-results'], wsData);
      // Flash new results
      const ids = new Set((wsData.results || []).map((r: OrderCandidate) => r.symbol));
      setFlashIds(ids);
      setTimeout(() => setFlashIds(new Set()), 1500);
    }
  }, [lastMessage, queryClient]);

  // API queries
  const { data: scanData } = useQuery({
    queryKey: ['scanner-results'],
    queryFn: getScannerResults,
    refetchInterval: 15000,
  });

  const { data: watchlistData } = useQuery({
    queryKey: ['scanner-watchlist'],
    queryFn: getWatchlist,
    refetchInterval: 60000,
  });

  const results = scanData?.results || [];
  const scannerRunning = scanData?.scanner_running ?? false;

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
          <Search className="w-5 h-5 text-gray-400" />
          <h1 className="text-xl font-semibold text-gray-900">Market Scanner</h1>
          <span
            className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              scannerRunning ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-500',
            )}
          >
            {scannerRunning ? 'Running' : 'Stopped'}
          </span>
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

      {/* Watchlist bar */}
      {watchlistData && (
        <div className="flex flex-wrap gap-2">
          {(watchlistData.symbols || []).map((sym) => (
            <span
              key={sym}
              className="px-3 py-1.5 bg-white border border-gray-200 rounded-full text-sm text-gray-700 min-h-[44px] flex items-center"
            >
              {sym}
              <span className="ml-1.5 text-xs text-gray-400">
                {watchlistData.weights?.[sym]?.toFixed(1) ?? '1.0'}
              </span>
            </span>
          ))}
        </div>
      )}

      {/* Results grid */}
      {results.length === 0 ? (
        <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
          <Crosshair className="w-8 h-8 text-gray-300 mx-auto mb-2" />
          <p className="text-sm text-gray-400">No scan results yet</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {results.map((c) => (
            <SymbolCard key={c.symbol} c={c} flash={flashIds.has(c.symbol)} />
          ))}
        </div>
      )}
    </div>
  );
}
