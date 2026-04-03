import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Brain, AlertCircle, CheckCircle, Clock } from 'lucide-react';
import { getAgentStatuses, getConfluenceSignals } from '../api/signals';
import type { AgentStatus, ConfluenceSignal } from '../api/signals';
import { useWSStore } from '../stores/wsStore';
import { cn, timeAgo } from '../lib/utils';

// ── Agent Card ──────────────────────────────────────────────
function AgentCard({ name, agent }: { name: string; agent: AgentStatus }) {
  const statusColor = agent.running
    ? 'bg-green-500'
    : agent.stale
      ? 'bg-yellow-500'
      : 'bg-red-500';

  const statusLabel = agent.running ? 'Running' : agent.stale ? 'Stale' : 'Error';

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      {/* Name + status */}
      <div className="flex items-center justify-between mb-3">
        <span className="font-medium text-sm text-gray-900 truncate">{name}</span>
        <div className="flex items-center gap-1.5">
          <span className={cn('w-2 h-2 rounded-full', statusColor)} />
          <span className="text-xs text-gray-500">{statusLabel}</span>
        </div>
      </div>

      {/* Signal bar */}
      <div className="mb-2">
        <div className="flex items-center justify-between text-xs mb-1">
          <span className="text-gray-400">Signal</span>
          <span className="font-mono text-gray-700">{agent.signal?.toFixed(2) ?? '—'}</span>
        </div>
        <div className="w-full bg-gray-100 rounded-full h-1.5">
          <div
            className="h-1.5 rounded-full transition-all"
            style={{
              width: `${Math.min((agent.signal ?? 0) * 100, 100)}%`,
              backgroundColor: `hsl(${(agent.signal ?? 0) * 120}, 70%, 50%)`,
            }}
          />
        </div>
      </div>

      {/* Confidence */}
      <div className="flex items-center justify-between text-xs mb-2">
        <span className="text-gray-400">Confidence</span>
        <span className="font-mono text-gray-700">{agent.confidence?.toFixed(2) ?? '—'}</span>
      </div>

      {/* Footer: updated + errors */}
      <div className="flex items-center justify-between text-xs">
        <span className="text-gray-400">
          <Clock className="w-3 h-3 inline mr-1" />
          {agent.updated_at ? timeAgo(agent.updated_at) : 'Never'}
        </span>
        {(agent.errors ?? 0) > 0 && (
          <span className="flex items-center gap-1 text-red-500">
            <AlertCircle className="w-3 h-3" />
            {agent.errors}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Signal Card ─────────────────────────────────────────────
function SignalCard({ signal }: { signal: ConfluenceSignal }) {
  const isLong = signal.direction === 'buy' || signal.direction === 'long';

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-4">
      {/* Symbol + direction */}
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-gray-900">{signal.symbol}</span>
        <span
          className={cn(
            'text-sm font-bold',
            isLong ? 'text-green-600' : 'text-red-600',
          )}
        >
          {isLong ? '▲ BUY' : '▼ SELL'}
        </span>
      </div>

      {/* Score bar */}
      <div className="mb-3">
        <div className="flex items-center justify-between text-xs mb-1">
          <span className="text-gray-400">Confluence Score</span>
          <span className="font-mono text-gray-700">{signal.score?.toFixed(2)}</span>
        </div>
        <div className="w-full bg-gray-100 rounded-full h-2">
          <div
            className="bg-blue-500 h-2 rounded-full transition-all"
            style={{ width: `${Math.min(signal.score * 100, 100)}%` }}
          />
        </div>
      </div>

      {/* Models + Regime */}
      <div className="flex flex-wrap gap-1 mb-2">
        {(signal.models || []).map((m) => (
          <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">
            {m}
          </span>
        ))}
        {signal.regime && (
          <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 text-xs rounded">
            {signal.regime}
          </span>
        )}
      </div>

      {/* Entry / SL / TP */}
      {signal.entry_price != null && (
        <div className="grid grid-cols-3 gap-2 text-xs mb-2">
          <div>
            <span className="text-gray-400">Entry</span>
            <p className="font-mono text-gray-900">{signal.entry_price.toFixed(2)}</p>
          </div>
          <div>
            <span className="text-gray-400">SL</span>
            <p className="font-mono text-red-600">{signal.stop_loss?.toFixed(2) ?? '—'}</p>
          </div>
          <div>
            <span className="text-gray-400">TP</span>
            <p className="font-mono text-green-600">{signal.take_profit?.toFixed(2) ?? '—'}</p>
          </div>
        </div>
      )}

      {/* Approval */}
      <div className="text-xs">
        {signal.approved ? (
          <span className="flex items-center gap-1 text-green-600">
            <CheckCircle className="w-3 h-3" /> Approved
          </span>
        ) : (
          <span className="flex items-center gap-1 text-red-500" title={signal.rejection_reason || ''}>
            <AlertCircle className="w-3 h-3" /> {signal.rejection_reason || 'Rejected'}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Intelligence Page ───────────────────────────────────────
export default function Intelligence() {
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (status === 'connected') {
      subscribe('signals');
    }
  }, [status, subscribe]);

  // WS updates → query cache
  useEffect(() => {
    const wsSignals = lastMessage['signals'];
    if (wsSignals) {
      queryClient.setQueryData(['agent-statuses'], wsSignals);
    }
  }, [lastMessage, queryClient]);

  const { data: agentData } = useQuery({
    queryKey: ['agent-statuses'],
    queryFn: getAgentStatuses,
    refetchInterval: 15000,
  });

  const { data: confluenceData } = useQuery({
    queryKey: ['confluence-signals'],
    queryFn: getConfluenceSignals,
    refetchInterval: 15000,
  });

  const agents = agentData ? Object.entries(agentData) : [];
  const activeCount = agents.filter(([, a]) => a.running).length;
  const signals = confluenceData?.signals || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Brain className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Intelligence</h1>
        <span className="px-2 py-0.5 rounded text-xs font-medium bg-blue-100 text-blue-700">
          {activeCount}/{agents.length} active
        </span>
      </div>

      {/* Agent Grid */}
      <div>
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide mb-3">Agents</h2>
        {agents.length === 0 ? (
          <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
            <Brain className="w-8 h-8 text-gray-300 mx-auto mb-2" />
            <p className="text-sm text-gray-400">No agent data available</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {agents.map(([name, agent]) => (
              <AgentCard key={name} name={name} agent={agent} />
            ))}
          </div>
        )}
      </div>

      {/* Confluence Signals */}
      <div>
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide mb-3">
          Confluence Signals
        </h2>
        {signals.length === 0 ? (
          <div className="bg-white rounded-lg border border-gray-200 p-8 text-center">
            <p className="text-sm text-gray-400">No active signals</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {signals.map((sig, i) => (
              <SignalCard key={`${sig.symbol}-${i}`} signal={sig} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
