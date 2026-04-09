import { useEffect, useMemo, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Wallet, X, XCircle, Search, SlidersHorizontal, ChevronLeft, ChevronRight } from 'lucide-react';
import { getPositions, closePosition, closeAllPositions, getTradeHistory } from '../api/trading';
import type { PaperPosition } from '../api/trading';
import { useWSStore } from '../stores/wsStore';
import { cn, formatUSD } from '../lib/utils';

const PER_PAGE = 50;

function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null) return '—';
  if (seconds <= 0) return '0s';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

function formatDateTime(iso: string | null | undefined): { date: string; time: string } {
  if (!iso) return { date: '—', time: '' };
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (isNaN(d.getTime())) return { date: '—', time: '' };
  const date = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  const time = d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  return { date, time };
}

function SideBadge({ side }: { side: string }) {
  const isLong = side === 'buy' || side === 'long';
  return (
    <span className={cn(
      'inline-flex items-center px-2 py-0.5 rounded text-xs font-semibold tracking-wide',
      isLong ? 'bg-emerald-50 text-emerald-700 border border-emerald-200' : 'bg-red-50 text-red-700 border border-red-200'
    )}>
      {isLong ? 'LONG' : 'SHORT'}
    </span>
  );
}

function StatCard({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div className="bg-white rounded-lg border border-gray-200 px-4 py-3 min-w-[110px]">
      <p className="text-xs text-gray-400 font-medium uppercase tracking-wide mb-0.5">{label}</p>
      <p className={cn('text-lg font-bold leading-tight', color || 'text-gray-900')}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-0.5">{sub}</p>}
    </div>
  );
}

function ExitReasonBadge({ reason }: { reason: string }) {
  if (!reason) return <span className="text-gray-300">—</span>;
  const map: Record<string, string> = {
    tp_hit: 'bg-emerald-50 text-emerald-700',
    take_profit: 'bg-emerald-50 text-emerald-700',
    sl_hit: 'bg-red-50 text-red-700',
    stop_loss: 'bg-red-50 text-red-700',
    partial_close: 'bg-amber-50 text-amber-700',
    manual: 'bg-blue-50 text-blue-700',
    crash_defense: 'bg-purple-50 text-purple-700',
    end_of_day: 'bg-gray-100 text-gray-600',
  };
  const cls = map[reason.toLowerCase()] || 'bg-gray-100 text-gray-600';
  const label = reason.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  return (
    <span className={cn('inline-flex px-2 py-0.5 rounded text-xs font-medium', cls)}>
      {label}
    </span>
  );
}

function ConfirmDialog({ open, title, message, onConfirm, onCancel }: {
  open: boolean; title: string; message: string; onConfirm: () => void; onCancel: () => void;
}) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-xl shadow-xl p-6 max-w-sm w-full mx-4">
        <h3 className="font-semibold text-gray-900 mb-2">{title}</h3>
        <p className="text-sm text-gray-600 mb-5">{message}</p>
        <div className="flex gap-3 justify-end">
          <button onClick={onCancel} className="px-4 py-2 rounded-lg text-sm font-medium text-gray-600 hover:bg-gray-100">Cancel</button>
          <button onClick={onConfirm} className="px-4 py-2 rounded-lg text-sm font-medium bg-red-600 text-white hover:bg-red-700">Confirm</button>
        </div>
      </div>
    </div>
  );
}

// ── Open Positions Tab ──────────────────────────────────────────────
function OpenPositionsTab({ positions, onClose, closingSymbol, onCloseAll }: {
  positions: PaperPosition[];
  onClose: (symbol: string) => void;
  closingSymbol: string | null;
  onCloseAll: () => void;
}) {
  const [search, setSearch] = useState('');

  const totalUnrealizedPnl = positions.reduce((s, p) => s + (p.unrealized_pnl ?? 0), 0);
  const longCount = positions.filter(p => p.side === 'buy' || p.side === 'long').length;
  const shortCount = positions.length - longCount;

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return positions.filter(p =>
      !q || p.symbol.toLowerCase().includes(q) || (p.models_fired || []).some(m => m.toLowerCase().includes(q))
    );
  }, [positions, search]);

  if (positions.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
        <Wallet className="w-10 h-10 text-gray-200 mx-auto mb-3" />
        <p className="text-sm text-gray-400 font-medium">No open positions</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Stats */}
      <div className="flex flex-wrap gap-3">
        <StatCard label="Open" value={String(positions.length)} sub={`${longCount}L · ${shortCount}S`} />
        <StatCard
          label="Unrealized P&L"
          value={formatUSD(totalUnrealizedPnl)}
          color={totalUnrealizedPnl >= 0 ? 'text-emerald-600' : 'text-red-600'}
        />
      </div>

      {/* Search + Close All */}
      <div className="flex gap-3 items-center">
        <div className="relative flex-1 max-w-xs">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search symbol or strategy…"
            className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        {positions.length > 0 && (
          <button onClick={onCloseAll} className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium text-red-600 hover:bg-red-50 rounded-lg border border-red-200">
            <XCircle className="w-3.5 h-3.5" /> Close All
          </button>
        )}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Symbol</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Side</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Entry</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Current Price</th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Exit Reason</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">P&L $</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">P&L %</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Duration</th>
                <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide"></th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((pos, i) => {
                const pnl = pos.unrealized_pnl ?? 0;
                const pnlPct = pos.unrealized_pnl_pct ?? 0;
                const entry = formatDateTime(pos.opened_at);
                // Duration since opened
                const durationS = pos.opened_at
                  ? Math.floor((Date.now() - new Date(pos.opened_at.endsWith('Z') ? pos.opened_at : pos.opened_at + 'Z').getTime()) / 1000)
                  : 0;
                return (
                  <tr key={pos.symbol} className={cn('border-b border-gray-100 hover:bg-gray-50 transition-colors', i % 2 === 0 ? '' : 'bg-gray-50/40')}>
                    <td className="px-4 py-3">
                      <div className="font-semibold text-gray-900">{pos.symbol.replace('/USDT', '')}<span className="text-gray-400 font-normal">/USDT</span></div>
                      {(pos.models_fired || []).length > 0 && (
                        <div className="text-xs text-blue-500 mt-0.5">{pos.models_fired[0]}</div>
                      )}
                    </td>
                    <td className="px-4 py-3"><SideBadge side={pos.side} /></td>
                    <td className="px-4 py-3 text-right">
                      <div className="font-mono font-semibold text-gray-900">{pos.entry_price?.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</div>
                      <div className="text-xs text-gray-400">{entry.date} {entry.time}</div>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className={cn('font-mono font-semibold', pnl >= 0 ? 'text-emerald-600' : 'text-red-600')}>
                        {pos.current_price?.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}
                      </div>
                      <div className="text-xs text-gray-400">live</div>
                    </td>
                    <td className="px-4 py-3"><span className="text-gray-300">—</span></td>
                    <td className={cn('px-4 py-3 text-right font-mono font-semibold', pnl >= 0 ? 'text-emerald-600' : 'text-red-600')}>
                      {pnl >= 0 ? '+' : ''}{formatUSD(pnl)}
                    </td>
                    <td className={cn('px-4 py-3 text-right font-mono', pnlPct >= 0 ? 'text-emerald-600' : 'text-red-600')}>
                      {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                    </td>
                    <td className="px-4 py-3 text-right text-gray-500 font-mono text-xs">
                      {formatDuration(durationS)}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <button
                        onClick={() => onClose(pos.symbol)}
                        disabled={closingSymbol === pos.symbol}
                        className="p-1.5 rounded-lg text-gray-300 hover:text-red-500 hover:bg-red-50 transition-colors"
                        title="Close position"
                      >
                        <X className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {filtered.length === 0 && (
          <div className="py-10 text-center text-sm text-gray-400">No matching positions</div>
        )}
      </div>
    </div>
  );
}

// ── Trade History Tab ───────────────────────────────────────────────
function TradeHistoryTab({ historyPage, onPageChange }: {
  historyPage: number;
  onPageChange: (p: number) => void;
}) {
  const [search, setSearch] = useState('');
  const [strategyFilter, setStrategyFilter] = useState('all');

  const { data, isLoading } = useQuery({
    queryKey: ['trade-history', historyPage],
    queryFn: () => getTradeHistory(historyPage, PER_PAGE),
    refetchInterval: 30000,
  });

  const trades = data?.trades || [];
  const total = data?.total ?? 0;
  const pages = data?.pages ?? 1;
  const summary = data?.summary;

  // Collect all unique strategies from current page
  const strategies = useMemo(() => {
    const set = new Set<string>();
    trades.forEach(t => (t.models_fired || []).forEach(m => set.add(m)));
    return Array.from(set).sort();
  }, [trades]);

  const filtered = useMemo(() => {
    const q = search.toLowerCase();
    return trades.filter(t => {
      const matchSearch = !q || t.symbol.toLowerCase().includes(q) || (t.models_fired || []).some(m => m.toLowerCase().includes(q));
      const matchStrategy = strategyFilter === 'all' || (t.models_fired || []).includes(strategyFilter);
      return matchSearch && matchStrategy;
    });
  }, [trades, search, strategyFilter]);

  const pnlColor = (summary?.total_pnl_usdt ?? 0) >= 0 ? 'text-emerald-600' : 'text-red-600';

  return (
    <div className="space-y-4">
      {/* Stats */}
      <div className="flex flex-wrap gap-3">
        <StatCard label="Total Trades" value={String(total)} />
        <StatCard
          label="Win / Loss"
          value={summary ? `${summary.wins} / ${summary.losses}` : '—'}
          sub={summary && total > 0 ? `${((summary.wins / (summary.wins + summary.losses)) * 100).toFixed(1)}% WR` : undefined}
        />
        <StatCard
          label="Total P&L"
          value={summary ? formatUSD(summary.total_pnl_usdt) : '—'}
          color={pnlColor}
        />
        <StatCard
          label="P&L %"
          value={summary ? `${summary.total_pnl_pct >= 0 ? '+' : ''}${summary.total_pnl_pct.toFixed(2)}%` : '—'}
          color={pnlColor}
        />
      </div>

      {/* Search + Filter */}
      <div className="flex flex-wrap gap-3 items-center">
        <div className="relative flex-1 min-w-[200px] max-w-xs">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-gray-400" />
          <input
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Search symbol or strategy…"
            className="w-full pl-9 pr-3 py-2 text-sm border border-gray-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div className="flex items-center gap-2">
          <SlidersHorizontal className="w-3.5 h-3.5 text-gray-400" />
          <select
            value={strategyFilter}
            onChange={e => setStrategyFilter(e.target.value)}
            className="text-sm border border-gray-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
          >
            <option value="all">All strategies</option>
            {strategies.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>
        <span className="text-xs text-gray-400 ml-auto">{filtered.length} of {total} trades</span>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {isLoading ? (
          <div className="py-16 text-center text-sm text-gray-400">Loading…</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Symbol</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Side</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Entry</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Exit</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Exit Reason</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">P&L $</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">P&L %</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-gray-500 uppercase tracking-wide">Duration</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t, i) => {
                  const pnl = t.pnl_usdt ?? 0;
                  const pnlPct = t.pnl_pct ?? 0;
                  const entry = formatDateTime(t.opened_at);
                  const exit = formatDateTime(t.closed_at);
                  return (
                    <tr key={i} className={cn('border-b border-gray-100 hover:bg-blue-50/30 transition-colors', i % 2 === 0 ? '' : 'bg-gray-50/40')}>
                      <td className="px-4 py-3">
                        <div className="font-semibold text-gray-900">{t.symbol.replace('/USDT', '')}<span className="text-gray-400 font-normal">/USDT</span></div>
                        {(t.models_fired || []).length > 0 && (
                          <div className="text-xs text-blue-500 mt-0.5">{t.models_fired[0]}</div>
                        )}
                      </td>
                      <td className="px-4 py-3"><SideBadge side={t.side} /></td>
                      <td className="px-4 py-3 text-right">
                        <div className="font-mono font-semibold text-gray-900">{t.entry_price?.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</div>
                        <div className="text-xs text-gray-400">{entry.date} {entry.time}</div>
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="font-mono font-semibold text-gray-900">{t.exit_price?.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })}</div>
                        <div className="text-xs text-gray-400">{exit.date} {exit.time}</div>
                      </td>
                      <td className="px-4 py-3"><ExitReasonBadge reason={t.exit_reason} /></td>
                      <td className={cn('px-4 py-3 text-right font-mono font-semibold', pnl >= 0 ? 'text-emerald-600' : 'text-red-600')}>
                        {pnl >= 0 ? '+' : ''}{formatUSD(pnl)}
                      </td>
                      <td className={cn('px-4 py-3 text-right font-mono', pnlPct >= 0 ? 'text-emerald-600' : 'text-red-600')}>
                        {pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%
                      </td>
                      <td className="px-4 py-3 text-right text-gray-500 font-mono text-xs">
                        {formatDuration(t.duration_s)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {!isLoading && filtered.length === 0 && (
          <div className="py-10 text-center text-sm text-gray-400">No trades found</div>
        )}

        {/* Pagination */}
        {pages > 1 && (
          <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100 bg-gray-50/50">
            <button
              onClick={() => onPageChange(Math.max(1, historyPage - 1))}
              disabled={historyPage <= 1}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm text-gray-600 hover:bg-white hover:border-gray-200 border border-transparent disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              <ChevronLeft className="w-4 h-4" /> Previous
            </button>
            <div className="flex items-center gap-1">
              {Array.from({ length: Math.min(pages, 7) }, (_, idx) => {
                const p = idx + 1;
                return (
                  <button
                    key={p}
                    onClick={() => onPageChange(p)}
                    className={cn(
                      'w-8 h-8 rounded-lg text-sm font-medium transition-colors',
                      p === historyPage ? 'bg-blue-600 text-white' : 'text-gray-600 hover:bg-gray-100'
                    )}
                  >
                    {p}
                  </button>
                );
              })}
              {pages > 7 && <span className="text-gray-400 px-1">…{pages}</span>}
            </div>
            <button
              onClick={() => onPageChange(Math.min(pages, historyPage + 1))}
              disabled={historyPage >= pages}
              className="flex items-center gap-1 px-3 py-1.5 rounded-lg text-sm text-gray-600 hover:bg-white hover:border-gray-200 border border-transparent disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
            >
              Next <ChevronRight className="w-4 h-4" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Main Trading Page ───────────────────────────────────────────────
export default function Trading() {
  const [activeTab, setActiveTab] = useState<'open' | 'history'>('open');
  const [historyPage, setHistoryPage] = useState(1);
  const [closingSymbol, setClosingSymbol] = useState<string | null>(null);
  const [confirmClose, setConfirmClose] = useState<string | null>(null);
  const [confirmCloseAll, setConfirmCloseAll] = useState(false);

  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (status === 'connected') { subscribe('positions'); subscribe('trades'); }
  }, [status, subscribe]);

  useEffect(() => {
    const wsPos = lastMessage['positions'];
    if (wsPos) queryClient.setQueryData(['trading-positions'], wsPos);
  }, [lastMessage, queryClient]);

  useEffect(() => {
    const wsTrade = lastMessage['trades'];
    if (wsTrade) {
      queryClient.invalidateQueries({ queryKey: ['trade-history'] });
      queryClient.invalidateQueries({ queryKey: ['trading-positions'] });
    }
  }, [lastMessage, queryClient]);

  const { data: posData } = useQuery({
    queryKey: ['trading-positions'],
    queryFn: getPositions,
    refetchInterval: 10000,
  });

  const positions = posData?.positions || [];
  const posCount = posData?.count ?? positions.length;

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
    <div className="max-w-7xl mx-auto px-4 py-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Trading</h1>
          <p className="text-sm text-gray-500 mt-0.5">Exchange positions and trade history</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-xl p-1 w-fit">
        <button
          onClick={() => setActiveTab('open')}
          className={cn(
            'px-5 py-2 rounded-lg text-sm font-medium transition-all',
            activeTab === 'open' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
          )}
        >
          Open Positions
          {posCount > 0 && (
            <span className="ml-2 px-1.5 py-0.5 bg-blue-100 text-blue-700 rounded-md text-xs font-semibold">{posCount}</span>
          )}
        </button>
        <button
          onClick={() => setActiveTab('history')}
          className={cn(
            'px-5 py-2 rounded-lg text-sm font-medium transition-all',
            activeTab === 'history' ? 'bg-white text-gray-900 shadow-sm' : 'text-gray-500 hover:text-gray-700'
          )}
        >
          Trade History
        </button>
      </div>

      {/* Tab Content */}
      {activeTab === 'open' ? (
        <OpenPositionsTab
          positions={positions}
          onClose={(s) => setConfirmClose(s)}
          closingSymbol={closingSymbol}
          onCloseAll={() => setConfirmCloseAll(true)}
        />
      ) : (
        <TradeHistoryTab historyPage={historyPage} onPageChange={setHistoryPage} />
      )}

      <ConfirmDialog
        open={!!confirmClose}
        title="Close Position"
        message={`Close ${confirmClose} position? This cannot be undone.`}
        onConfirm={() => confirmClose && handleClose(confirmClose)}
        onCancel={() => setConfirmClose(null)}
      />
      <ConfirmDialog
        open={confirmCloseAll}
        title="Close All Positions"
        message={`Close all ${posCount} open positions? This cannot be undone.`}
        onConfirm={handleCloseAll}
        onCancel={() => setConfirmCloseAll(false)}
      />
    </div>
  );
}
