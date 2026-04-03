import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Wallet, X, XCircle } from 'lucide-react';
import { getPositions, closePosition, closeAllPositions, getTradeHistory } from '../api/trading';
import type { PaperPosition, ClosedTrade } from '../api/trading';
import { useWSStore } from '../stores/wsStore';
import { cn, formatUSD, formatPct, timeAgo } from '../lib/utils';

// ── Duration formatter ──────────────────────────────────────
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

// ── Opened-at to duration ───────────────────────────────────
function durationSince(openedAt: string): string {
  const s = Math.floor((Date.now() - new Date(openedAt).getTime()) / 1000);
  return formatDuration(Math.max(s, 0));
}

// ── Confirm dialog ──────────────────────────────────────────
function ConfirmDialog({
  open,
  title,
  message,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-lg shadow-xl p-6 max-w-sm w-full mx-4">
        <h3 className="font-semibold text-gray-900 mb-2">{title}</h3>
        <p className="text-sm text-gray-600 mb-4">{message}</p>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg text-sm font-medium text-gray-600 hover:bg-gray-100 min-h-[44px]"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700 min-h-[44px]"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Position Card ───────────────────────────────────────────
function PositionCard({
  pos,
  onClose,
  closing,
}: {
  pos: PaperPosition;
  onClose: (symbol: string) => void;
  closing: boolean;
}) {
  const isLong = pos.side === 'buy' || pos.side === 'long';
  const pnlColor = pos.unrealized_pnl >= 0 ? 'text-green-600' : 'text-red-600';

  return (
    <div
      className={cn(
        'bg-white rounded-lg border border-gray-200 p-4 transition-opacity',
        closing && 'opacity-40',
      )}
    >
      {/* Row 1: Symbol + Side + Close button */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className="font-semibold text-gray-900">{pos.symbol}</span>
          <span
            className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              isLong ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700',
            )}
          >
            {isLong ? 'LONG' : 'SHORT'}
          </span>
          {pos.regime && (
            <span className="px-2 py-0.5 rounded text-xs bg-gray-100 text-gray-500">
              {pos.regime}
            </span>
          )}
        </div>
        <button
          onClick={() => onClose(pos.symbol)}
          disabled={closing}
          className="p-2 rounded-lg text-gray-400 hover:text-red-600 hover:bg-red-50 min-w-[44px] min-h-[44px] flex items-center justify-center"
          title="Close position"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Row 2: Entry → Current */}
      <div className="flex items-center gap-2 mb-2 text-sm">
        <span className="text-gray-500">Entry</span>
        <span className="font-mono text-gray-900">{pos.entry_price?.toFixed(2)}</span>
        <span className="text-gray-400">→</span>
        <span className={cn('font-mono font-medium', pnlColor)}>
          {pos.current_price?.toFixed(2)}
        </span>
      </div>

      {/* Row 3: PnL */}
      <div className="flex items-center gap-4 mb-3">
        <div>
          <span className="text-xs text-gray-400">Unrealized PnL</span>
          <p className={cn('font-mono font-semibold', pnlColor)}>
            {formatUSD(pos.unrealized_pnl ?? 0)}{' '}
            <span className="text-xs">
              ({formatPct(pos.unrealized_pnl_pct ?? 0)})
            </span>
          </p>
        </div>
        <div>
          <span className="text-xs text-gray-400">Size</span>
          <p className="font-mono text-gray-900">{formatUSD(pos.size_usdt ?? 0)}</p>
        </div>
      </div>

      {/* Row 4: SL / TP */}
      <div className="grid grid-cols-2 gap-2 text-xs mb-2">
        <div>
          <span className="text-gray-400">Stop Loss</span>
          <p className="font-mono text-red-600">{pos.stop_loss?.toFixed(2) ?? '—'}</p>
        </div>
        <div>
          <span className="text-gray-400">Take Profit</span>
          <p className="font-mono text-green-600">{pos.take_profit?.toFixed(2) ?? '—'}</p>
        </div>
      </div>

      {/* Row 5: Models + indicators */}
      <div className="flex flex-wrap gap-1 mb-2">
        {(pos.models_fired || []).map((m) => (
          <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">
            {m}
          </span>
        ))}
        {pos.auto_partial_applied && (
          <span className="px-1.5 py-0.5 bg-amber-50 text-amber-600 text-xs rounded">
            Partial applied
          </span>
        )}
        {pos.breakeven_applied && (
          <span className="px-1.5 py-0.5 bg-indigo-50 text-indigo-600 text-xs rounded">
            Breakeven SL
          </span>
        )}
      </div>

      {/* Row 6: Duration */}
      <p className="text-xs text-gray-400">Opened {durationSince(pos.opened_at)} ago</p>
    </div>
  );
}

// ── Trade History Table ─────────────────────────────────────
function TradeHistoryTable({
  trades,
  total,
  page,
  pages,
  onPageChange,
}: {
  trades: ClosedTrade[];
  total: number;
  page: number;
  pages: number;
  onPageChange: (p: number) => void;
}) {
  // Summary
  const winCount = trades.filter((t) => t.pnl_usdt > 0).length;
  const totalPnl = trades.reduce((s, t) => s + (t.pnl_usdt ?? 0), 0);
  const winRate = trades.length > 0 ? (winCount / trades.length) * 100 : 0;

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      {/* Summary row */}
      <div className="flex flex-wrap gap-4 mb-4 text-sm">
        <span className="text-gray-500">
          Total: <span className="font-medium text-gray-900">{total}</span>
        </span>
        <span className="text-gray-500">
          Win Rate: <span className="font-medium text-gray-900">{winRate.toFixed(1)}%</span>
        </span>
        <span className="text-gray-500">
          PnL:{' '}
          <span className={cn('font-mono font-medium', totalPnl >= 0 ? 'text-green-600' : 'text-red-600')}>
            {formatUSD(totalPnl)}
          </span>
        </span>
      </div>

      {/* Table */}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500">
              <th className="pb-2 font-medium">Symbol</th>
              <th className="pb-2 font-medium">Side</th>
              <th className="pb-2 font-medium text-right">Entry→Exit</th>
              <th className="pb-2 font-medium text-right">PnL</th>
              <th className="pb-2 font-medium text-right">Duration</th>
              <th className="pb-2 font-medium">Exit Reason</th>
              <th className="pb-2 font-medium text-right">When</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => (
              <tr key={i} className="border-t border-gray-100">
                <td className="py-2 font-medium text-gray-900">{t.symbol}</td>
                <td className={cn('py-2', t.side === 'buy' ? 'text-green-600' : 'text-red-600')}>
                  {t.side.toUpperCase()}
                </td>
                <td className="py-2 text-right font-mono text-xs text-gray-700">
                  {t.entry_price?.toFixed(2)}→{t.exit_price?.toFixed(2)}
                </td>
                <td
                  className={cn(
                    'py-2 text-right font-mono',
                    t.pnl_usdt >= 0 ? 'text-green-600' : 'text-red-600',
                  )}
                >
                  {formatUSD(t.pnl_usdt)}
                </td>
                <td className="py-2 text-right text-gray-500">{formatDuration(t.duration_s ?? 0)}</td>
                <td className="py-2 text-gray-500 text-xs">{t.exit_reason}</td>
                <td className="py-2 text-right text-gray-400">{timeAgo(t.closed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Pagination */}
      {pages > 1 && (
        <div className="flex items-center justify-between mt-4">
          <button
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page <= 1}
            className="px-3 py-2 rounded-lg text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-30 min-h-[44px]"
          >
            Previous
          </button>
          <span className="text-sm text-gray-500">
            Page {page} of {pages}
          </span>
          <button
            onClick={() => onPageChange(Math.min(pages, page + 1))}
            disabled={page >= pages}
            className="px-3 py-2 rounded-lg text-sm text-gray-600 hover:bg-gray-100 disabled:opacity-30 min-h-[44px]"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}

// ── Trading Page ────────────────────────────────────────────
export default function Trading() {
  const [activeTab, setActiveTab] = useState<'open' | 'history'>('open');
  const [historyPage, setHistoryPage] = useState(1);
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);
  const [confirmClose, setConfirmClose] = useState<string | null>(null);
  const [confirmCloseAll, setConfirmCloseAll] = useState(false);

  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  // WS subscriptions
  useEffect(() => {
    if (status === 'connected') {
      subscribe('positions');
      subscribe('trades');
    }
  }, [status, subscribe]);

  // WS position updates → query cache
  useEffect(() => {
    const wsPos = lastMessage['positions'];
    if (wsPos) {
      queryClient.setQueryData(['trading-positions'], wsPos);
    }
  }, [lastMessage, queryClient]);

  // WS trade close events → refetch history
  useEffect(() => {
    const wsTrade = lastMessage['trades'];
    if (wsTrade) {
      queryClient.invalidateQueries({ queryKey: ['trade-history'] });
      queryClient.invalidateQueries({ queryKey: ['trading-positions'] });
    }
  }, [lastMessage, queryClient]);

  // API queries
  const { data: posData } = useQuery({
    queryKey: ['trading-positions'],
    queryFn: getPositions,
    refetchInterval: 10000,
  });

  const { data: historyData } = useQuery({
    queryKey: ['trade-history', historyPage],
    queryFn: () => getTradeHistory(historyPage, 20),
    refetchInterval: 30000,
  });

  const positions = posData?.positions || [];
  const posCount = posData?.count ?? positions.length;

  // Close handlers
  const handleClose = async (symbol: string) => {
    setClosingSymbol(symbol);
    try {
      await closePosition(symbol);
      queryClient.invalidateQueries({ queryKey: ['trading-positions'] });
    } finally {
      setTimeout(() => setClosingSymbol(null), 500);
    }
    setConfirmClose(null);
  };

  const handleCloseAll = async () => {
    try {
      await closeAllPositions();
      queryClient.invalidateQueries({ queryKey: ['trading-positions'] });
    } finally {
      setConfirmCloseAll(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Wallet className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Paper Trading</h1>
      </div>

      {/* Tab bar */}
      <div className="flex items-center justify-between">
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
          <button
            onClick={() => setActiveTab('open')}
            className={cn(
              'px-4 py-2 rounded-md text-sm font-medium transition-colors min-h-[44px]',
              activeTab === 'open' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500',
            )}
          >
            Open Positions
            {posCount > 0 && (
              <span className="ml-2 px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded text-xs">
                {posCount}
              </span>
            )}
          </button>
          <button
            onClick={() => setActiveTab('history')}
            className={cn(
              'px-4 py-2 rounded-md text-sm font-medium transition-colors min-h-[44px]',
              activeTab === 'history' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500',
            )}
          >
            Trade History
          </button>
        </div>

        {activeTab === 'open' && positions.length > 0 && (
          <button
            onClick={() => setConfirmCloseAll(true)}
            className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium text-red-600 hover:bg-red-50 min-h-[44px]"
          >
            <XCircle className="w-4 h-4" />
            Close All
          </button>
        )}
      </div>

      {/* Tab content */}
      {activeTab === 'open' ? (
        positions.length === 0 ? (
          <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
            <Wallet className="w-8 h-8 text-gray-300 mx-auto mb-2" />
            <p className="text-sm text-gray-400">No open positions</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {positions.map((pos) => (
              <PositionCard
                key={pos.symbol}
                pos={pos}
                onClose={(s) => setConfirmClose(s)}
                closing={closingSymbol === pos.symbol}
              />
            ))}
          </div>
        )
      ) : (
        <TradeHistoryTable
          trades={historyData?.trades || []}
          total={historyData?.total ?? 0}
          page={historyData?.page ?? 1}
          pages={historyData?.pages ?? 1}
          onPageChange={setHistoryPage}
        />
      )}

      {/* Confirm close single */}
      <ConfirmDialog
        open={!!confirmClose}
        title="Close Position"
        message={`Close ${confirmClose} position? This action cannot be undone.`}
        onConfirm={() => confirmClose && handleClose(confirmClose)}
        onCancel={() => setConfirmClose(null)}
      />

      {/* Confirm close all */}
      <ConfirmDialog
        open={confirmCloseAll}
        title="Close All Positions"
        message={`Close all ${posCount} open positions? This action cannot be undone.`}
        onConfirm={handleCloseAll}
        onCancel={() => setConfirmCloseAll(false)}
      />
    </div>
  );
}
