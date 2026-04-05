import { useEffect } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getDashboardSummary, getCrashDefense, getSystemHealth } from '../api/dashboard';
import { getMonitorPortfolio } from '../api/monitor';
import { getExchanges } from '../api/exchanges';
import { getAgentStatuses } from '../api/signals';
import { useWSStore } from '../stores/wsStore';
import { formatUSD, formatPct, timeAgo, cn } from '../lib/utils';

// ── Active strategies (signal models used in the scan pipeline)
const ACTIVE_STRATEGIES = [
  { name: 'Momentum Breakout', key: 'momentum_breakout', status: 'active' },
  { name: 'Pullback Long', key: 'pullback_long', status: 'active' },
  { name: 'Swing Low Continuation', key: 'swing_low_continuation', status: 'active' },
  { name: 'Funding Rate', key: 'funding_rate', status: 'active' },
  { name: 'Sentiment', key: 'sentiment', status: 'active' },
  { name: 'RL Ensemble', key: 'rl_ensemble', status: 'active' },
  { name: 'Trend Model', key: 'trend', status: 'disabled' },
  { name: 'Donchian Breakout', key: 'donchian_breakout', status: 'disabled' },
];

function StatCard({ label, value, sub, color }: {
  label: string; value: string; sub?: string; color?: 'green' | 'red' | 'yellow' | 'default';
}) {
  const colorClass = {
    green: 'text-green-600', red: 'text-red-600', yellow: 'text-yellow-600', default: 'text-gray-900',
  }[color || 'default'];

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">{label}</p>
      <p className={cn('text-2xl font-bold mt-1', colorClass)}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
    </div>
  );
}

function CrashDefenseCard({ tier, score, isDefensive }: { tier: string; score: number; isDefensive: boolean }) {
  const tierColor: Record<string, string> = {
    NORMAL: 'bg-green-100 text-green-700', DEFENSIVE: 'bg-yellow-100 text-yellow-700',
    HIGH_ALERT: 'bg-orange-100 text-orange-700', EMERGENCY: 'bg-red-100 text-red-700',
    SYSTEMIC: 'bg-red-200 text-red-800',
  };
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">Crash Defense</p>
      <div className="flex items-center gap-2 mt-2">
        <span className={cn('px-2 py-0.5 rounded text-xs font-bold', tierColor[tier] || 'bg-gray-100 text-gray-600')}>{tier}</span>
        <span className="text-sm text-gray-600">Score: {score.toFixed(1)}</span>
      </div>
      {isDefensive && <p className="text-xs text-yellow-600 mt-1">Defensive mode active</p>}
    </div>
  );
}

function RecentTradesTable({ trades }: {
  trades: Array<{ symbol: string; side: string; pnl_usdt: number; closed_at: string }>;
}) {
  if (!trades || trades.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Recent Trades</p>
        <p className="text-sm text-gray-400">No recent trades</p>
      </div>
    );
  }
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Recent Trades</p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-900 uppercase tracking-wider">
              <th className="pb-2 font-semibold">Symbol</th>
              <th className="pb-2 font-semibold">Side</th>
              <th className="pb-2 font-semibold text-right">PnL</th>
              <th className="pb-2 font-semibold text-right">When</th>
            </tr>
          </thead>
          <tbody>
            {trades.slice(0, 10).map((trade, i) => (
              <tr key={i} className="border-t border-gray-100">
                <td className="py-1.5 font-medium text-gray-900">{trade.symbol}</td>
                <td className={cn('py-1.5 font-medium', (trade.side === 'buy' || trade.side === 'long') ? 'text-green-600' : 'text-red-600')}>
                  {(trade.side === 'buy' || trade.side === 'long') ? 'LONG' : 'SHORT'}
                </td>
                <td className={cn('py-1.5 text-right font-mono', trade.pnl_usdt >= 0 ? 'text-green-600' : 'text-red-600')}>
                  {formatUSD(trade.pnl_usdt)}
                </td>
                <td className="py-1.5 text-right text-gray-400">{timeAgo(trade.closed_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SystemStatusCard({ health, exchangeName, agentCount }: {
  health: { exchange: string; database: string; threads: number; scanner: string; uptime: number } | undefined;
  exchangeName: string;
  agentCount: number;
}) {
  if (!health) return null;
  const uptimeHours = Math.floor((health.uptime || 0) / 3600);
  const uptimeMin = Math.floor(((health.uptime || 0) % 3600) / 60);

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">System Status</p>
      <div className="space-y-2 text-sm">
        <div className="flex justify-between">
          <span className="text-gray-500">Exchange</span>
          <span className="font-medium text-gray-900">{exchangeName}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Connection</span>
          <span className={health.exchange === 'ok' ? 'text-green-600 font-medium' : 'text-red-600 font-medium'}>
            {health.exchange === 'ok' ? 'Connected' : 'Disconnected'}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Database</span>
          <span className={health.database === 'ok' ? 'text-green-600 font-medium' : 'text-red-600 font-medium'}>
            {health.database === 'ok' ? 'Connected' : 'Error'}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Scanner</span>
          <span className="font-medium text-gray-900">{health.scanner}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">AI Agents Running</span>
          <span className="font-medium text-gray-900">{agentCount}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Threads</span>
          <span className={cn('font-medium', health.threads > 75 ? 'text-yellow-600' : 'text-gray-900')}>{health.threads}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Uptime</span>
          <span className="font-medium text-gray-900">{uptimeHours}h {uptimeMin}m</span>
        </div>
      </div>
    </div>
  );
}

function StrategiesCard() {
  const active = ACTIVE_STRATEGIES.filter(s => s.status === 'active');
  const disabled = ACTIVE_STRATEGIES.filter(s => s.status === 'disabled');

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">
        Active Strategies <span className="text-gray-400 ml-1">{active.length}/{ACTIVE_STRATEGIES.length}</span>
      </p>
      <div className="space-y-1.5">
        {active.map(s => (
          <div key={s.key} className="flex items-center gap-2 text-sm">
            <span className="w-1.5 h-1.5 rounded-full bg-green-500" />
            <span className="text-gray-900">{s.name}</span>
          </div>
        ))}
        {disabled.map(s => (
          <div key={s.key} className="flex items-center gap-2 text-sm">
            <span className="w-1.5 h-1.5 rounded-full bg-gray-300" />
            <span className="text-gray-400 line-through">{s.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function Dashboard() {
  const { connect, subscribe, lastMessage, status } = useWSStore();

  useEffect(() => { connect(); }, [connect]);
  useEffect(() => {
    if (status === 'connected') { subscribe('dashboard'); subscribe('crash_defense'); }
  }, [status, subscribe]);

  const { data: summary } = useQuery({ queryKey: ['dashboard-summary'], queryFn: getDashboardSummary, refetchInterval: 10000 });
  const { data: crashDefense } = useQuery({ queryKey: ['crash-defense'], queryFn: getCrashDefense, refetchInterval: 15000 });
  const { data: health } = useQuery({ queryKey: ['system-health'], queryFn: getSystemHealth, refetchInterval: 30000 });
  const { data: portfolioData } = useQuery({ queryKey: ['monitor-portfolio'], queryFn: getMonitorPortfolio, refetchInterval: 30000 });
  const { data: exchangesData } = useQuery({ queryKey: ['exchanges-list'], queryFn: getExchanges, staleTime: 60000 });
  const { data: agentData } = useQuery({ queryKey: ['agent-statuses'], queryFn: getAgentStatuses, refetchInterval: 30000 });

  const dashData = lastMessage['dashboard'] || summary;
  const crashData = lastMessage['crash_defense'] || crashDefense;

  const pnl = dashData?.pnl ?? 0;
  const capital = dashData?.capital ?? 0;
  const drawdown = dashData?.drawdown ?? 0;
  const positions = dashData?.positions ?? dashData?.open_count ?? 0;
  const winRate = dashData?.win_rate ?? 0;
  const pf = dashData?.profit_factor ?? 0;
  const capitalDeployed = portfolioData?.portfolio?.used_margin ?? 0;
  const activeExchange = exchangesData?.find((e: any) => e.is_active);
  const exchangeName = activeExchange ? `${activeExchange.name}` : 'Not connected';
  const agentCount = agentData ? Object.keys(agentData).length : 0;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-900">Dashboard</h1>
        <div className="flex items-center gap-2">
          <span className={cn('w-2 h-2 rounded-full', status === 'connected' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500' : 'bg-gray-300')} />
          <span className="text-xs text-gray-500">
            {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting...' : 'Offline'}
          </span>
        </div>
      </div>

      {/* Stats Grid — row 1 */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <StatCard label="Capital" value={formatUSD(capital)} />
        <StatCard label="Capital Deployed" value={formatUSD(capitalDeployed)} sub={`${positions} positions`} />
        <StatCard label="PnL" value={formatUSD(pnl)} color={pnl >= 0 ? 'green' : 'red'} />
        <StatCard label="Drawdown" value={formatPct(drawdown)} color={Math.abs(drawdown) > 5 ? 'red' : Math.abs(drawdown) > 2 ? 'yellow' : 'default'} />
        <StatCard label="Open Positions" value={String(positions)} />
      </div>

      {/* Stats Grid — row 2 */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
        <StatCard label="Win Rate" value={formatPct(winRate)} sub="All closed trades" />
        <StatCard label="Profit Factor" value={pf.toFixed(2)} sub="Gross profit / loss" />
        {crashData && (
          <CrashDefenseCard tier={crashData.tier || 'NORMAL'} score={crashData.score ?? 0} isDefensive={crashData.is_defensive ?? false} />
        )}
      </div>

      {/* Bottom: Trades + Strategies + System Status */}
      <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
        <div className="lg:col-span-2">
          <RecentTradesTable trades={dashData?.recent_trades || []} />
        </div>
        <StrategiesCard />
        <SystemStatusCard health={health} exchangeName={exchangeName} agentCount={agentCount} />
      </div>
    </div>
  );
}
