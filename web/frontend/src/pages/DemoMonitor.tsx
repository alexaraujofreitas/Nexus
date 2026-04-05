import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import {
  getMonitorPositions,
  getMonitorPortfolio,
  getMonitorPnL,
  getMonitorRisk,
  getMonitorTrades,
  type MonitorPosition,
  type MonitorTrade,
} from '../api/monitor';
import { getCurrentRegime } from '../api/analytics';
import { useWSStore } from '../stores/wsStore';
import { formatUSD, formatPct, timeAgo, cn } from '../lib/utils';

// ── Helper: Format duration ────────────────────────────
function formatDuration(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
  }
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  return `${d}d ${h}h`;
}

// ── Component: Portfolio Summary Bar ───────────────────
function PortfolioSummary({ portfolio, pnl }: { portfolio: any; pnl: any }) {
  if (!portfolio || !pnl) {
    return (
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="bg-white rounded-xl border border-gray-200 p-4 animate-pulse">
            <div className="h-3 bg-gray-200 rounded w-1/2 mb-2" />
            <div className="h-6 bg-gray-200 rounded w-3/4" />
          </div>
        ))}
      </div>
    );
  }

  const stats = [
    { label: 'Equity',          value: formatUSD(portfolio.equity),             color: 'default' as const },
    { label: 'Daily PnL',       value: formatUSD(pnl.daily_pnl),               color: (pnl.daily_pnl >= 0 ? 'green' : 'red') as const },
    { label: 'Unrealized PnL',  value: formatUSD(pnl.total_unrealized),         color: (pnl.total_unrealized >= 0 ? 'green' : 'red') as const },
    { label: 'Realized PnL',    value: formatUSD(pnl.total_realized),           color: (pnl.total_realized >= 0 ? 'green' : 'red') as const },
    { label: 'Portfolio Heat',   value: formatPct(portfolio.portfolio_heat_pct), color: (portfolio.portfolio_heat_pct > 5 ? 'red' : portfolio.portfolio_heat_pct > 3 ? 'yellow' : 'green') as const },
    { label: 'Fees Paid',       value: formatUSD(pnl.fees_paid),                color: 'default' as const },
  ];

  const colorMap = { green: 'text-green-600', red: 'text-red-600', yellow: 'text-yellow-600', default: 'text-gray-900' };

  return (
    <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-4">
      {stats.map((s) => (
        <div key={s.label} className="bg-white rounded-xl border border-gray-200 p-4">
          <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">{s.label}</p>
          <p className={cn('text-lg font-bold mt-1.5', colorMap[s.color])}>{s.value}</p>
        </div>
      ))}
    </div>
  );
}

// ── Component: Active Positions Table ──────────────────
function ActivePositionsTable({ positions }: { positions: MonitorPosition[] }) {
  if (!positions || positions.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-4">Active Positions</p>
        <p className="text-sm text-gray-400">No active positions</p>
      </div>
    );
  }

  const sorted = [...positions].sort((a, b) => b.pnl_unrealized - a.pnl_unrealized);

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-4">
        Active Positions ({sorted.length})
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-[11px] text-gray-900 uppercase tracking-wider border-b border-gray-200">
              <th className="pb-3 px-3 text-center font-semibold">Symbol</th>
              <th className="pb-3 px-3 text-center font-semibold">Side</th>
              <th className="pb-3 px-3 text-center font-semibold">Entry</th>
              <th className="pb-3 px-3 text-center font-semibold">Current</th>
              <th className="pb-3 px-3 text-center font-semibold">PnL $</th>
              <th className="pb-3 px-3 text-center font-semibold">PnL %</th>
              <th className="pb-3 px-3 text-center font-semibold">Duration</th>
              <th className="pb-3 px-3 text-center font-semibold">Regime</th>
              <th className="pb-3 px-3 text-center font-semibold">SL / TP</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((pos, idx) => (
              <tr key={idx} className="border-b border-gray-50 hover:bg-gray-50/60 transition-colors">
                <td className="py-3 px-3 text-center font-semibold text-gray-900">{pos.symbol}</td>
                <td className={cn('py-3 px-3 text-center font-semibold', pos.side === 'long' ? 'text-green-600' : 'text-red-600')}>
                  {pos.side.toUpperCase()}
                </td>
                <td className="py-3 px-3 text-center font-mono text-xs text-gray-700">{formatUSD(pos.entry_price)}</td>
                <td className="py-3 px-3 text-center font-mono text-xs text-gray-700">{formatUSD(pos.current_price)}</td>
                <td className={cn('py-3 px-3 text-center font-mono text-xs font-semibold', pos.pnl_unrealized >= 0 ? 'text-green-600' : 'text-red-600')}>
                  {formatUSD(pos.pnl_unrealized)}
                </td>
                <td className={cn('py-3 px-3 text-center font-mono text-xs font-semibold', pos.pnl_unrealized >= 0 ? 'text-green-600' : 'text-red-600')}>
                  {formatPct(pos.pnl_pct)}
                </td>
                <td className="py-3 px-3 text-center font-mono text-xs text-gray-500">{formatDuration(pos.duration_s)}</td>
                <td className="py-3 px-3 text-center text-xs text-gray-500">{pos.regime_at_entry}</td>
                <td className="py-3 px-3 text-center font-mono text-xs text-gray-500 whitespace-nowrap">
                  {pos.stop_loss !== null ? formatUSD(pos.stop_loss) : '—'}
                  <span className="text-gray-300 mx-1">/</span>
                  {pos.take_profit !== null ? formatUSD(pos.take_profit) : '—'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Component: Risk Panel ──────────────────────────────
function RiskPanel({ risk, regime }: { risk: any; regime: any }) {
  if (!risk) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-6 animate-pulse">
        <div className="space-y-4">
          {[...Array(5)].map((_, i) => (
            <div key={i}>
              <div className="h-3 bg-gray-200 rounded w-1/3 mb-2" />
              <div className="h-4 bg-gray-200 rounded w-full" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  const tierColor: Record<string, string> = {
    NORMAL: 'bg-green-100 text-green-700',
    DEFENSIVE: 'bg-yellow-100 text-yellow-700',
    HIGH_ALERT: 'bg-orange-100 text-orange-700',
    EMERGENCY: 'bg-red-100 text-red-700',
    SYSTEMIC: 'bg-red-200 text-red-800',
  };

  const regimeColors: Record<string, string> = {
    bull_trend: '#16a34a', bear_trend: '#dc2626', ranging: '#ca8a04',
    vol_expansion: '#7c3aed', uncertain: '#6b7280',
  };
  const regimeLabels: Record<string, string> = {
    bull_trend: 'Bull Trend', bear_trend: 'Bear Trend', ranging: 'Ranging',
    vol_expansion: 'Vol Expansion', uncertain: 'Uncertain',
  };

  return (
    <div className="space-y-4">
      {regime && (
        <div className="bg-white rounded-xl border border-gray-200 p-4">
          <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Market Regime</p>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="w-3 h-3 rounded-full" style={{ backgroundColor: regimeColors[regime.regime] || '#6b7280' }} />
              <span className="font-semibold text-gray-900">{regimeLabels[regime.regime] || regime.regime}</span>
            </div>
            <span className="text-sm font-mono text-gray-500">{formatPct(regime.confidence)}</span>
          </div>
        </div>
      )}

      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-4">Risk Status</p>
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-gray-600">Drawdown</span>
            <span className="text-xs font-mono text-gray-700">{formatPct(risk.drawdown_pct)}</span>
          </div>
          <div className="w-full bg-gray-100 rounded-full h-1.5">
            <div className={cn('h-1.5 rounded-full transition-all', Math.abs(risk.drawdown_pct) > 10 ? 'bg-red-500' : Math.abs(risk.drawdown_pct) > 5 ? 'bg-orange-400' : 'bg-green-500')}
              style={{ width: `${Math.min(Math.abs(risk.drawdown_pct) / 20 * 100, 100)}%` }} />
          </div>
        </div>
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-gray-600">Daily Loss</span>
            <span className="text-xs font-mono text-gray-700">{formatPct(risk.daily_loss_pct)}</span>
          </div>
          <div className="w-full bg-gray-100 rounded-full h-1.5">
            <div className={cn('h-1.5 rounded-full transition-all', risk.daily_loss_pct > 5 ? 'bg-red-500' : risk.daily_loss_pct > 2 ? 'bg-orange-400' : 'bg-green-500')}
              style={{ width: `${Math.min(risk.daily_loss_pct / 10 * 100, 100)}%` }} />
          </div>
        </div>
        <div className="space-y-2.5 pt-1">
          {[
            { label: 'Circuit Breaker', value: risk.circuit_breaker_triggered ? 'TRIGGERED' : 'OK', ok: !risk.circuit_breaker_triggered },
            { label: 'Trading', value: risk.trading_enabled ? 'ENABLED' : 'DISABLED', ok: risk.trading_enabled },
            { label: 'Crash Defense', value: risk.crash_defense_tier, ok: risk.crash_defense_tier === 'NORMAL' },
          ].map((r) => (
            <div key={r.label} className="flex items-center justify-between">
              <span className="text-xs text-gray-600">{r.label}</span>
              <span className={cn('px-2 py-0.5 rounded text-[11px] font-semibold', r.ok ? 'bg-green-50 text-green-700' : tierColor[r.value] || 'bg-red-50 text-red-700')}>
                {r.value}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ── Component: Recent Trades Table ─────────────────────
function RecentTradesTable({ trades }: { trades: MonitorTrade[] }) {
  if (!trades || trades.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-4">Recent Trades</p>
        <p className="text-sm text-gray-400">No recent trades</p>
      </div>
    );
  }

  const sorted = [...trades].sort((a, b) => new Date(b.closed_at).getTime() - new Date(a.closed_at).getTime());

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-5">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-4">Recent Trades (Last 50)</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="text-[11px] text-gray-900 uppercase tracking-wider border-b border-gray-200">
              <th className="pb-3 px-3 text-center font-semibold">Time</th>
              <th className="pb-3 px-3 text-center font-semibold">Symbol</th>
              <th className="pb-3 px-3 text-center font-semibold">Side</th>
              <th className="pb-3 px-3 text-center font-semibold">Entry</th>
              <th className="pb-3 px-3 text-center font-semibold">Exit</th>
              <th className="pb-3 px-3 text-center font-semibold">PnL $</th>
              <th className="pb-3 px-3 text-center font-semibold">PnL %</th>
              <th className="pb-3 px-3 text-center font-semibold">R-Multiple</th>
              <th className="pb-3 px-3 text-center font-semibold">Duration</th>
              <th className="pb-3 px-3 text-center font-semibold">Regime</th>
              <th className="pb-3 px-3 text-center font-semibold">Exit Reason</th>
            </tr>
          </thead>
          <tbody>
            {sorted.slice(0, 50).map((trade, idx) => {
              const isLong = trade.side === 'buy' || trade.side === 'long';
              return (
                <tr key={idx} className="border-b border-gray-50 hover:bg-gray-50/60 transition-colors">
                  <td className="py-2.5 px-3 text-center text-gray-500 font-mono text-xs">{timeAgo(trade.closed_at)}</td>
                  <td className="py-2.5 px-3 text-center font-semibold text-gray-900">{trade.symbol}</td>
                  <td className={cn('py-2.5 px-3 text-center font-semibold', isLong ? 'text-green-600' : 'text-red-600')}>
                    {isLong ? 'LONG' : 'SHORT'}
                  </td>
                  <td className="py-2.5 px-3 text-center font-mono text-xs text-gray-700">{formatUSD(trade.entry_price)}</td>
                  <td className="py-2.5 px-3 text-center font-mono text-xs text-gray-700">{formatUSD(trade.exit_price)}</td>
                  <td className={cn('py-2.5 px-3 text-center font-mono text-xs font-semibold', trade.pnl_usdt >= 0 ? 'text-green-600' : 'text-red-600')}>
                    {formatUSD(trade.pnl_usdt)}
                  </td>
                  <td className={cn('py-2.5 px-3 text-center font-mono text-xs font-semibold', (trade.pnl_pct ?? 0) >= 0 ? 'text-green-600' : 'text-red-600')}>
                    {formatPct(trade.pnl_pct ?? 0)}
                  </td>
                  <td className={cn('py-2.5 px-3 text-center font-mono text-xs font-semibold', trade.r_multiple >= 0 ? 'text-green-600' : 'text-red-600')}>
                    {trade.r_multiple.toFixed(2)}R
                  </td>
                  <td className="py-2.5 px-3 text-center font-mono text-xs text-gray-500">{formatDuration(trade.duration_s)}</td>
                  <td className="py-2.5 px-3 text-center text-xs text-gray-500">{trade.regime}</td>
                  <td className="py-2.5 px-3 text-center text-xs text-gray-500">{trade.exit_reason}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────
export default function DemoMonitor() {
  const { connect, subscribe, lastMessage, status } = useWSStore();

  useEffect(() => { connect(); }, [connect]);
  useEffect(() => {
    if (status === 'connected') {
      subscribe('positions'); subscribe('dashboard');
      subscribe('crash_defense'); subscribe('risk'); subscribe('monitor');
    }
  }, [status, subscribe]);

  const { data: positionsData } = useQuery({ queryKey: ['monitor-positions'], queryFn: getMonitorPositions, refetchInterval: 30000 });
  const { data: portfolioData } = useQuery({ queryKey: ['monitor-portfolio'], queryFn: getMonitorPortfolio, refetchInterval: 30000 });
  const { data: pnlData } = useQuery({ queryKey: ['monitor-pnl'], queryFn: getMonitorPnL, refetchInterval: 30000 });
  const { data: riskData } = useQuery({ queryKey: ['monitor-risk'], queryFn: getMonitorRisk, refetchInterval: 30000 });
  const { data: tradesData } = useQuery({ queryKey: ['monitor-trades'], queryFn: getMonitorTrades, refetchInterval: 30000 });
  const { data: regimeData } = useQuery({ queryKey: ['current-regime'], queryFn: getCurrentRegime, refetchInterval: 60000 });

  const positions = lastMessage['positions']?.positions || positionsData?.positions || [];
  const portfolio = lastMessage['dashboard'] || portfolioData?.portfolio;
  const pnl = lastMessage['monitor'] || pnlData?.pnl;
  const risk = lastMessage['risk'] || riskData?.risk;
  const trades = tradesData?.trades || [];
  const regime = regimeData;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">Trades</h1>
          <p className="text-sm text-gray-500 mt-0.5">Real-time paper trading monitor</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={cn('w-2 h-2 rounded-full', status === 'connected' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500' : 'bg-gray-300')} />
          <span className="text-xs text-gray-500">
            {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting...' : 'Offline'}
          </span>
        </div>
      </div>

      <PortfolioSummary portfolio={portfolio} pnl={pnl} />

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <ActivePositionsTable positions={positions} />
        </div>
        <RiskPanel risk={risk} regime={regime} />
      </div>

      <RecentTradesTable trades={trades} />
    </div>
  );
}
