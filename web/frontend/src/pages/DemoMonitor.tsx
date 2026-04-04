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
function PortfolioSummary({ portfolio, pnl }: {
  portfolio: any;
  pnl: any;
}) {
  if (!portfolio || !pnl) {
    return (
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
        {[...Array(6)].map((_, i) => (
          <div key={i} className="bg-white rounded-lg border border-gray-200 p-4 animate-pulse">
            <div className="h-3 bg-gray-200 rounded w-1/2 mb-2" />
            <div className="h-6 bg-gray-200 rounded w-full" />
          </div>
        ))}
      </div>
    );
  }

  const stats = [
    {
      label: 'Equity',
      value: formatUSD(portfolio.equity),
      color: 'default',
    },
    {
      label: 'Daily PnL',
      value: formatUSD(pnl.daily_pnl),
      color: pnl.daily_pnl >= 0 ? 'green' : 'red',
    },
    {
      label: 'Unrealized PnL',
      value: formatUSD(pnl.total_unrealized),
      color: pnl.total_unrealized >= 0 ? 'green' : 'red',
    },
    {
      label: 'Realized PnL',
      value: formatUSD(pnl.total_realized),
      color: pnl.total_realized >= 0 ? 'green' : 'red',
    },
    {
      label: 'Portfolio Heat',
      value: formatPct(portfolio.portfolio_heat_pct),
      color: portfolio.portfolio_heat_pct > 5 ? 'red' : portfolio.portfolio_heat_pct > 3 ? 'yellow' : 'green',
    },
    {
      label: 'Fees Paid',
      value: formatUSD(pnl.fees_paid),
      color: 'default',
    },
  ];

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-6 gap-4">
      {stats.map((stat) => (
        <div key={stat.label} className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">{stat.label}</p>
          <p className={cn(
            'text-lg font-semibold mt-2',
            stat.color === 'green' && 'text-green-600',
            stat.color === 'red' && 'text-red-600',
            stat.color === 'yellow' && 'text-yellow-600',
            stat.color === 'default' && 'text-gray-900',
          )}>
            {stat.value}
          </p>
        </div>
      ))}
    </div>
  );
}

// ── Component: Active Positions Table ──────────────────
function ActivePositionsTable({ positions }: { positions: MonitorPosition[] }) {
  if (!positions || positions.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">Active Positions</p>
        <p className="text-sm text-gray-400">No active positions</p>
      </div>
    );
  }

  // Sort by PnL descending
  const sorted = [...positions].sort((a, b) => b.pnl_unrealized - a.pnl_unrealized);

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">Active Positions ({sorted.length})</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
              <th className="pb-3 font-medium">Symbol</th>
              <th className="pb-3 font-medium">Side</th>
              <th className="pb-3 font-medium text-right">Entry</th>
              <th className="pb-3 font-medium text-right">Current</th>
              <th className="pb-3 font-medium text-right">PnL $</th>
              <th className="pb-3 font-medium text-right">PnL %</th>
              <th className="pb-3 font-medium">Duration</th>
              <th className="pb-3 font-medium">Regime</th>
              <th className="pb-3 font-medium">SL / TP</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((pos, idx) => (
              <tr key={idx} className={cn(
                'border-b border-gray-100 hover:bg-gray-50',
                pos.pnl_unrealized >= 0 ? 'bg-green-50/30' : 'bg-red-50/30',
              )}>
                <td className="py-3 font-medium text-gray-900">{pos.symbol}</td>
                <td className={cn(
                  'py-3 font-medium',
                  pos.side === 'long' ? 'text-green-600' : 'text-red-600',
                )}>
                  {pos.side.toUpperCase()}
                </td>
                <td className="py-3 text-right text-gray-700 font-mono text-xs">
                  {pos.entry_price.toFixed(2)}
                </td>
                <td className="py-3 text-right text-gray-700 font-mono text-xs">
                  {pos.current_price.toFixed(2)}
                </td>
                <td className={cn(
                  'py-3 text-right font-mono font-medium text-xs',
                  pos.pnl_unrealized >= 0 ? 'text-green-600' : 'text-red-600',
                )}>
                  {formatUSD(pos.pnl_unrealized)}
                </td>
                <td className={cn(
                  'py-3 text-right font-mono font-medium text-xs',
                  pos.pnl_unrealized >= 0 ? 'text-green-600' : 'text-red-600',
                )}>
                  {formatPct(pos.pnl_pct)}
                </td>
                <td className="py-3 text-gray-600 text-xs">{formatDuration(pos.duration_s)}</td>
                <td className="py-3 text-gray-600 text-xs">{pos.regime_at_entry}</td>
                <td className="py-3 text-gray-600 text-xs">
                  {pos.stop_loss !== null ? pos.stop_loss.toFixed(2) : '—'}
                  {' / '}
                  {pos.take_profit !== null ? pos.take_profit.toFixed(2) : '—'}
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
function RiskPanel({ risk, regime }: {
  risk: any;
  regime: any;
}) {
  if (!risk) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6 animate-pulse">
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
    bull_trend: '#16a34a',
    bear_trend: '#dc2626',
    ranging: '#ca8a04',
    vol_expansion: '#7c3aed',
    vol_compression: '#8b5cf6',
    accumulation: '#22c55e',
    distribution: '#f87171',
    uncertain: '#6b7280',
  };

  const regimeLabels: Record<string, string> = {
    bull_trend: 'Bull Trend',
    bear_trend: 'Bear Trend',
    ranging: 'Ranging',
    vol_expansion: 'Vol Expansion',
    vol_compression: 'Vol Compression',
    accumulation: 'Accumulation',
    distribution: 'Distribution',
    uncertain: 'Uncertain',
  };

  const regimeColor = regime ? regimeColors[regime.regime] || '#6b7280' : '#6b7280';
  const regimeLabel = regime ? regimeLabels[regime.regime] || regime.regime : 'Unknown';

  return (
    <div className="space-y-4">
      {/* Regime Overlay */}
      {regime && (
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-3">Market Regime</p>
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div
                className="w-3 h-3 rounded-full"
                style={{ backgroundColor: regimeColor }}
              />
              <span className="font-medium text-gray-900">{regimeLabel}</span>
            </div>
            <span className="text-sm font-mono text-gray-600">{formatPct(regime.confidence)}</span>
          </div>
        </div>
      )}

      {/* Risk Panel */}
      <div className="bg-white rounded-lg border border-gray-200 p-4">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">Risk Status</p>

        {/* Drawdown Bar */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-gray-600">Drawdown</span>
            <span className="text-xs font-mono text-gray-700">{formatPct(risk.drawdown_pct)}</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div
              className={cn(
                'h-2 rounded-full transition-all',
                Math.abs(risk.drawdown_pct) > 10 ? 'bg-red-600' :
                Math.abs(risk.drawdown_pct) > 5 ? 'bg-orange-500' :
                'bg-green-600',
              )}
              style={{ width: `${Math.min(Math.abs(risk.drawdown_pct) / 20 * 100, 100)}%` }}
            />
          </div>
        </div>

        {/* Daily Loss Bar */}
        <div className="mb-4">
          <div className="flex items-center justify-between mb-1">
            <span className="text-xs text-gray-600">Daily Loss</span>
            <span className="text-xs font-mono text-gray-700">{formatPct(risk.daily_loss_pct)}</span>
          </div>
          <div className="w-full bg-gray-200 rounded-full h-2">
            <div
              className={cn(
                'h-2 rounded-full transition-all',
                risk.daily_loss_pct > 5 ? 'bg-red-600' :
                risk.daily_loss_pct > 2 ? 'bg-orange-500' :
                'bg-green-600',
              )}
              style={{ width: `${Math.min(risk.daily_loss_pct / 10 * 100, 100)}%` }}
            />
          </div>
        </div>

        {/* Status Badges */}
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-600">Circuit Breaker</span>
            <span className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              risk.circuit_breaker_triggered
                ? 'bg-red-100 text-red-700'
                : 'bg-green-100 text-green-700',
            )}>
              {risk.circuit_breaker_triggered ? 'TRIGGERED' : 'OK'}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-600">Trading</span>
            <span className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              risk.trading_enabled
                ? 'bg-green-100 text-green-700'
                : 'bg-red-100 text-red-700',
            )}>
              {risk.trading_enabled ? 'ENABLED' : 'DISABLED'}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-xs text-gray-600">Crash Defense</span>
            <span className={cn(
              'px-2 py-0.5 rounded text-xs font-medium',
              tierColor[risk.crash_defense_tier] || 'bg-gray-100 text-gray-600',
            )}>
              {risk.crash_defense_tier}
            </span>
          </div>
        </div>

        {/* Disable Reason */}
        {!risk.trading_enabled && risk.reason && (
          <div className="mt-3 pt-3 border-t border-gray-200">
            <p className="text-xs text-gray-600">
              <span className="font-medium">Reason: </span>
              {risk.reason}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Component: Recent Trades Table ─────────────────────
function RecentTradesTable({ trades }: { trades: MonitorTrade[] }) {
  if (!trades || trades.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">Recent Trades</p>
        <p className="text-sm text-gray-400">No recent trades</p>
      </div>
    );
  }

  // Sort by closed_at descending (newest first)
  const sorted = [...trades].sort((a, b) => new Date(b.closed_at).getTime() - new Date(a.closed_at).getTime());

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">Recent Trades (Last 50)</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
              <th className="pb-3 font-medium">Time</th>
              <th className="pb-3 font-medium">Symbol</th>
              <th className="pb-3 font-medium">Side</th>
              <th className="pb-3 font-medium text-right">Entry</th>
              <th className="pb-3 font-medium text-right">Exit</th>
              <th className="pb-3 font-medium text-right">PnL $</th>
              <th className="pb-3 font-medium text-right">R-Multiple</th>
              <th className="pb-3 font-medium">Duration</th>
              <th className="pb-3 font-medium">Regime</th>
              <th className="pb-3 font-medium">Exit Reason</th>
            </tr>
          </thead>
          <tbody>
            {sorted.slice(0, 50).map((trade, idx) => (
              <tr key={idx} className={cn(
                'border-b border-gray-100 hover:bg-gray-50',
                trade.pnl_usdt >= 0 ? 'bg-green-50/20' : 'bg-red-50/20',
              )}>
                <td className="py-2 text-gray-600 font-mono text-xs">{timeAgo(trade.closed_at)}</td>
                <td className="py-2 font-medium text-gray-900">{trade.symbol}</td>
                <td className={cn(
                  'py-2 font-medium',
                  trade.side === 'long' ? 'text-green-600' : 'text-red-600',
                )}>
                  {trade.side.toUpperCase()}
                </td>
                <td className="py-2 text-right text-gray-700 font-mono text-xs">
                  {trade.entry_price.toFixed(2)}
                </td>
                <td className="py-2 text-right text-gray-700 font-mono text-xs">
                  {trade.exit_price.toFixed(2)}
                </td>
                <td className={cn(
                  'py-2 text-right font-mono font-medium text-xs',
                  trade.pnl_usdt >= 0 ? 'text-green-600' : 'text-red-600',
                )}>
                  {formatUSD(trade.pnl_usdt)}
                </td>
                <td className={cn(
                  'py-2 text-right font-mono font-medium text-xs',
                  trade.r_multiple >= 0 ? 'text-green-600' : 'text-red-600',
                )}>
                  {trade.r_multiple.toFixed(2)}R
                </td>
                <td className="py-2 text-gray-600 text-xs">{formatDuration(trade.duration_s)}</td>
                <td className="py-2 text-gray-600 text-xs">{trade.regime}</td>
                <td className="py-2 text-gray-600 text-xs">{trade.exit_reason}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Main Page ──────────────────────────────────────────
export default function DemoMonitor() {
  const { connect, subscribe, lastMessage, status } = useWSStore();

  // Connect WebSocket
  useEffect(() => {
    connect();
    return () => {
      // Don't disconnect on unmount — keep WS alive
    };
  }, [connect]);

  // Subscribe to channels
  useEffect(() => {
    if (status === 'connected') {
      subscribe('positions');
      subscribe('dashboard');
      subscribe('crash_defense');
      subscribe('risk');
      subscribe('monitor');
    }
  }, [status, subscribe]);

  // REST queries with polling fallback
  const { data: positionsData } = useQuery({
    queryKey: ['monitor-positions'],
    queryFn: getMonitorPositions,
    refetchInterval: 30000,
  });

  const { data: portfolioData } = useQuery({
    queryKey: ['monitor-portfolio'],
    queryFn: getMonitorPortfolio,
    refetchInterval: 30000,
  });

  const { data: pnlData } = useQuery({
    queryKey: ['monitor-pnl'],
    queryFn: getMonitorPnL,
    refetchInterval: 30000,
  });

  const { data: riskData } = useQuery({
    queryKey: ['monitor-risk'],
    queryFn: getMonitorRisk,
    refetchInterval: 30000,
  });

  const { data: tradesData } = useQuery({
    queryKey: ['monitor-trades'],
    queryFn: getMonitorTrades,
    refetchInterval: 30000,
  });

  const { data: regimeData } = useQuery({
    queryKey: ['current-regime'],
    queryFn: getCurrentRegime,
    refetchInterval: 60000,
  });

  // Merge WS + REST
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
          <h1 className="text-2xl font-bold text-gray-900">Demo Monitor</h1>
          <p className="text-sm text-gray-500 mt-1">Real-time paper trading dashboard (Phase 1)</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={cn(
            'w-2 h-2 rounded-full',
            status === 'connected' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500' : 'bg-gray-300',
          )} />
          <span className="text-xs text-gray-500">
            {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting...' : 'Offline'}
          </span>
        </div>
      </div>

      {/* Portfolio Summary Bar */}
      <PortfolioSummary portfolio={portfolio} pnl={pnl} />

      {/* Main Grid: Positions (2/3) + Risk Panel (1/3) */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-2">
          <ActivePositionsTable positions={positions} />
        </div>
        <RiskPanel risk={risk} regime={regime} />
      </div>

      {/* Recent Trades Table */}
      <RecentTradesTable trades={trades} />
    </div>
  );
}
