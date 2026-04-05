import { useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Brain, AlertCircle, CheckCircle, Clock, Activity } from 'lucide-react';
import { getConfluenceSignals } from '../api/signals';
import type { ConfluenceSignal } from '../api/signals';
import { useWSStore } from '../stores/wsStore';
import { cn, timeAgo } from '../lib/utils';

// ── AI Agents definition (actual intelligence agents, not signal models) ──
const AI_AGENTS = [
  { name: 'Funding Rate Agent', key: 'funding_rate', category: 'Market Microstructure', description: 'Monitors perpetual funding rates across exchanges for sentiment divergence', enabled: true },
  { name: 'News Agent', key: 'news', category: 'Sentiment', description: 'Processes crypto news feeds (CryptoPanic, RSS) via NLP for market impact scoring', enabled: true },
  { name: 'Macro Agent', key: 'macro', category: 'Macro', description: 'Tracks macro indicators (DXY, bond yields, VIX) for risk-on/risk-off signals', enabled: true },
  { name: 'Geopolitical Agent', key: 'geopolitical', category: 'Macro', description: 'Monitors geopolitical events and sanctions risk affecting crypto markets', enabled: true },
  { name: 'On-Chain Agent', key: 'onchain', category: 'On-Chain', description: 'Tracks wallet flows, exchange deposits/withdrawals, and whale movements', enabled: true },
  { name: 'Whale Tracking Agent', key: 'whale', category: 'On-Chain', description: 'Detects large wallet transfers and exchange inflow/outflow anomalies', enabled: true },
  { name: 'Liquidation Flow Agent', key: 'liquidation', category: 'Market Microstructure', description: 'Monitors liquidation cascades and open interest changes via Coinglass', enabled: true },
  { name: 'Crash Detection Agent', key: 'crash_detection', category: 'Risk', description: 'Multi-component crash scorer with 7 risk factors and 4-tier response', enabled: true },
  { name: 'Squeeze Detection Agent', key: 'squeeze', category: 'Market Microstructure', description: 'Detects short/long squeeze setups from funding + OI + price divergence', enabled: true },
  { name: 'Stablecoin Liquidity Agent', key: 'stablecoin', category: 'Liquidity', description: 'Tracks USDT/USDC supply changes as leading indicators of capital flow', enabled: true },
  { name: 'Position Monitor Agent', key: 'position_monitor', category: 'Execution', description: 'Watches open positions for stop/target proximity and exit timing', enabled: true },
  { name: 'FinBERT Sentiment Agent', key: 'finbert', category: 'Sentiment', description: 'GPU-accelerated transformer model scoring crypto news sentiment in real-time', enabled: true },
  { name: 'Order Book Agent', key: 'orderbook', category: 'Market Microstructure', description: 'Analyzes bid/ask depth and order book imbalances', enabled: false },
  { name: 'Options Flow Agent', key: 'options', category: 'Derivatives', description: 'Monitors options open interest and max pain for directional bias', enabled: false },
  { name: 'Social Sentiment Agent', key: 'social', category: 'Sentiment', description: 'Aggregates sentiment from Twitter, Reddit, and Telegram', enabled: false },
  { name: 'Volatility Surface Agent', key: 'vol_surface', category: 'Derivatives', description: 'Tracks implied volatility skew and term structure changes', enabled: false },
  { name: 'Sector Rotation Agent', key: 'sector_rotation', category: 'Macro', description: 'Detects capital rotation between crypto sectors (DeFi, L1, L2, memes)', enabled: false },
  { name: 'Narrative Shift Agent', key: 'narrative', category: 'Sentiment', description: 'Detects narrative regime shifts in crypto media discourse', enabled: false },
  { name: 'Miner Flow Agent', key: 'miner_flow', category: 'On-Chain', description: 'Tracks Bitcoin miner wallet outflows as selling pressure indicator', enabled: false },
  { name: 'Twitter Sentiment Agent', key: 'twitter', category: 'Sentiment', description: 'Real-time Twitter/X sentiment scoring for crypto assets', enabled: false },
  { name: 'Reddit Sentiment Agent', key: 'reddit', category: 'Sentiment', description: 'Monitors Reddit crypto communities for sentiment shifts', enabled: false },
  { name: 'Scalping Agent', key: 'scalp', category: 'Execution', description: 'Short-timeframe scalping signal generation for quick trades', enabled: false },
  { name: 'Liquidity Vacuum Agent', key: 'liquidity_vacuum', category: 'Market Microstructure', description: 'Detects liquidity voids and stop-hunt zones in the order book', enabled: false },
];

// ── Agent Card ──
function AgentCard({ agent }: { agent: typeof AI_AGENTS[0] }) {
  return (
    <div className={cn(
      'bg-white rounded-xl border p-4 transition-colors',
      agent.enabled ? 'border-gray-200' : 'border-gray-100 opacity-60',
    )}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-sm text-gray-900">{agent.name}</span>
        <div className="flex items-center gap-1.5">
          <span className={cn('w-2 h-2 rounded-full', agent.enabled ? 'bg-green-500' : 'bg-gray-300')} />
          <span className="text-xs text-gray-500">{agent.enabled ? 'Active' : 'Disabled'}</span>
        </div>
      </div>
      <span className="inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-blue-50 text-blue-600 mb-2">
        {agent.category}
      </span>
      <p className="text-xs text-gray-500 leading-relaxed">{agent.description}</p>
    </div>
  );
}

// ── Signal Card ──
function SignalCard({ signal }: { signal: ConfluenceSignal }) {
  const isLong = signal.direction === 'buy' || signal.direction === 'long';
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-gray-900">{signal.symbol}</span>
        <span className={cn('text-sm font-bold', isLong ? 'text-green-600' : 'text-red-600')}>
          {isLong ? '▲ LONG' : '▼ SHORT'}
        </span>
      </div>
      <div className="mb-3">
        <div className="flex items-center justify-between text-xs mb-1">
          <span className="text-gray-400">Confluence Score</span>
          <span className="font-mono text-gray-700">{signal.score?.toFixed(2)}</span>
        </div>
        <div className="w-full bg-gray-100 rounded-full h-2">
          <div className="bg-blue-500 h-2 rounded-full transition-all" style={{ width: `${Math.min(signal.score * 100, 100)}%` }} />
        </div>
      </div>
      <div className="flex flex-wrap gap-1 mb-2">
        {(signal.models || []).map((m) => (
          <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">{m}</span>
        ))}
        {signal.regime && <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 text-xs rounded">{signal.regime}</span>}
      </div>
      <div className="text-xs">
        {signal.approved ? (
          <span className="flex items-center gap-1 text-green-600"><CheckCircle className="w-3 h-3" /> Approved</span>
        ) : (
          <span className="flex items-center gap-1 text-red-500" title={signal.rejection_reason || ''}>
            <AlertCircle className="w-3 h-3" /> {signal.rejection_reason || 'No signal'}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Intelligence Page ──
export default function Intelligence() {
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => { if (status === 'connected') subscribe('signals'); }, [status, subscribe]);
  useEffect(() => {
    const ws = lastMessage['signals'];
    if (ws) queryClient.setQueryData(['confluence-signals'], ws);
  }, [lastMessage, queryClient]);

  const { data: confluenceData } = useQuery({
    queryKey: ['confluence-signals'], queryFn: getConfluenceSignals, refetchInterval: 15000,
  });

  const enabledAgents = AI_AGENTS.filter(a => a.enabled);
  const disabledAgents = AI_AGENTS.filter(a => !a.enabled);
  const signals = confluenceData?.signals || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Brain className="w-5 h-5 text-blue-500" />
        <h1 className="text-xl font-bold text-gray-900">Intelligence</h1>
        <span className="px-2 py-0.5 rounded text-xs font-bold bg-blue-100 text-blue-700">
          {enabledAgents.length}/{AI_AGENTS.length} active
        </span>
      </div>

      {/* Active Agents */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Active AI Agents</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {enabledAgents.map((agent) => <AgentCard key={agent.key} agent={agent} />)}
        </div>
      </div>

      {/* Disabled Agents */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Disabled AI Agents</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {disabledAgents.map((agent) => <AgentCard key={agent.key} agent={agent} />)}
        </div>
      </div>

      {/* Confluence Signals */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Confluence Signals</h2>
        {signals.length === 0 ? (
          <div className="bg-white rounded-xl border border-gray-200 p-8 text-center">
            <Activity className="w-8 h-8 text-gray-200 mx-auto mb-2" />
            <p className="text-sm text-gray-400">No active signals — waiting for next scan cycle</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
            {signals.map((sig, i) => <SignalCard key={`${sig.symbol}-${i}`} signal={sig} />)}
          </div>
        )}
      </div>
    </div>
  );
}
