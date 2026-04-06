import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  RefreshCw, CheckCircle, XCircle, AlertCircle, BookOpen, ChevronDown,
} from 'lucide-react';
import { getDashboardSummary, getCrashDefense, getSystemHealth } from '../api/dashboard';
import { getMonitorPortfolio } from '../api/monitor';
import { getExchanges } from '../api/exchanges';
import { getAgentStatuses } from '../api/signals';
import { getValidationHealth, getReadiness, getDataIntegrity } from '../api/validation';
import { getPipelineStatus } from '../api/scanner';
import { useWSStore } from '../stores/wsStore';
import { formatUSD, formatPct, cn } from '../lib/utils';

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

const STATUS_ICONS: Record<string, typeof CheckCircle> = {
  ok: CheckCircle,
  warning: AlertCircle,
  error: XCircle,
};

// ── Reusable Components ────────────────────────────────────

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

// ── Unified System Health Card ─────────────────────────────
// Merges System Status + Component Health into one card.
// Static info (exchange name, agent count) plus dynamic
// component health checks with proper status icons.

function StatusRow({ label, value, valueClass }: { label: string; value: string; valueClass?: string }) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-sm text-gray-500">{label}</span>
      <span className={cn('text-sm font-medium', valueClass || 'text-gray-900')}>{value}</span>
    </div>
  );
}

function SystemHealthCard({
  health,
  components,
  threadCount,
  uptimeSeconds,
  scannerRunning,
  lastScanAt,
}: {
  health: { exchange: string; database: string; threads: number; scanner: string; uptime: number } | undefined;
  components: Record<string, { status: string; detail?: string }>;
  threadCount?: number;
  uptimeSeconds?: number;
  scannerRunning?: boolean;
  lastScanAt?: string;
}) {
  const threads = threadCount ?? health?.threads ?? 0;
  const uptime = uptimeSeconds ?? health?.uptime ?? 0;
  const uptimeHours = Math.floor(uptime / 3600);
  const uptimeMin = Math.floor((uptime % 3600) / 60);

  const hasComponents = Object.keys(components).length > 0;

  // Format last scan time
  const lastScanFormatted = (() => {
    if (!lastScanAt) return '--';
    try {
      const d = new Date(lastScanAt.endsWith('Z') ? lastScanAt : lastScanAt + 'Z');
      if (isNaN(d.getTime())) return '--';
      return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    } catch { return '--'; }
  })();

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 h-full">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-2">System Health</p>

      <div className="divide-y divide-gray-50">
        {/* Component health rows (from validation API), excluding items shown elsewhere */}
        {hasComponents ? (
          Object.entries(components)
            .filter(([name]) => name !== 'threads' && name !== 'exchange' && name !== 'executor' && name !== 'scanner')
            .map(([name, comp]) => {
              const Icon = STATUS_ICONS[comp.status] || AlertCircle;
              const color = comp.status === 'ok' ? 'text-green-600' : comp.status === 'warning' ? 'text-amber-600' : 'text-red-600';
              return (
                <div key={name} className="flex items-center justify-between py-1.5">
                  <div className="flex items-center gap-1.5">
                    <Icon className={cn('w-3.5 h-3.5', color)} />
                    <span className="text-sm text-gray-500 capitalize">{name}</span>
                  </div>
                  <div className="text-right">
                    <span className={cn('text-sm font-medium', color)}>
                      {comp.detail || comp.status}
                    </span>
                  </div>
                </div>
              );
            })
        ) : health ? (
          <>
            <StatusRow
              label="Connection"
              value={health.exchange === 'ok' ? 'Connected' : 'Disconnected'}
              valueClass={health.exchange === 'ok' ? 'text-green-600' : 'text-red-600'}
            />
            <StatusRow
              label="Database"
              value={health.database === 'ok' ? 'Connected' : 'Error'}
              valueClass={health.database === 'ok' ? 'text-green-600' : 'text-red-600'}
            />
          </>
        ) : null}

        {/* Scanner with status + last scan time */}
        <div className="flex items-center justify-between py-1.5">
          <span className="text-sm text-gray-500">Scanner</span>
          <div className="text-right">
            <span className={cn('text-sm font-medium', scannerRunning ? 'text-green-600' : 'text-amber-600')}>
              {scannerRunning ? 'Running' : 'Stopped'}
            </span>
            <p className="text-[11px] text-gray-400">Last scan: {lastScanFormatted}</p>
          </div>
        </div>

        <StatusRow
          label="Threads"
          value={String(threads)}
          valueClass={threads > 75 ? 'text-amber-600' : undefined}
        />
        <StatusRow label="Uptime" value={`${uptimeHours}h ${uptimeMin}m`} />
      </div>
    </div>
  );
}

// ── AI Agents definition (matches Intelligence page — keys match agent._name in code) ───────
const AI_AGENTS = [
  // Enabled agents (15 active in production)
  { name: 'Funding Rate', key: 'funding_rate', enabled: true },
  { name: 'News', key: 'news', enabled: true },
  { name: 'Macro', key: 'macro', enabled: true },
  { name: 'Geopolitical', key: 'geopolitical', enabled: true },
  { name: 'On-Chain', key: 'onchain', enabled: true },
  { name: 'Whale Tracking', key: 'whale', enabled: true },
  { name: 'Liquidation Flow', key: 'liquidation_flow', enabled: true },
  { name: 'Crash Detection', key: 'crash_detection', enabled: true },
  { name: 'Squeeze Detection', key: 'squeeze_detection', enabled: true },
  { name: 'Stablecoin Liquidity', key: 'stablecoin', enabled: true },
  { name: 'Position Monitor', key: 'position_monitor', enabled: true },
  { name: 'Telegram Sentiment', key: 'telegram', enabled: true },
  { name: 'Order Book', key: 'order_book', enabled: true },
  { name: 'Options Flow', key: 'options_flow', enabled: true },
  { name: 'Volatility Surface', key: 'volatility_surface', enabled: true },
  { name: 'Social Sentiment', key: 'social_sentiment', enabled: true },
  { name: 'Sector Rotation', key: 'sector_rotation', enabled: true },
  { name: 'Narrative Shift', key: 'narrative_shift', enabled: true },
  { name: 'Miner Flow', key: 'miner_flow', enabled: true },
  { name: 'Twitter Sentiment', key: 'twitter', enabled: true },
  { name: 'Reddit Sentiment', key: 'reddit', enabled: true },
  { name: 'Scalping', key: 'scalp', enabled: true },
  { name: 'Liquidity Vacuum', key: 'liquidity_vacuum', enabled: true },
];

function AIAgentsCard({ liveStatuses }: { liveStatuses: Record<string, any> | undefined }) {
  const enabledAgents = AI_AGENTS.filter(a => a.enabled);
  const disabledAgents = AI_AGENTS.filter(a => !a.enabled);

  // Derive dot color + tooltip from live API status
  function agentDot(agent: typeof AI_AGENTS[number]) {
    const live = liveStatuses?.[agent.key];
    if (!live) return { dot: 'bg-gray-400', tip: 'No data' };
    if (live.errors > 0) return { dot: 'bg-red-500', tip: `${live.errors} error${live.errors > 1 ? 's' : ''}` };
    if (live.stale) return { dot: 'bg-amber-500', tip: 'Stale' };
    // Green = running + has fresh data + contributing to orchestrator/dependent model
    if (live.running && live.has_data) return { dot: 'bg-green-500', tip: 'Active' };
    // Running but no data — agent is alive but not contributing
    if (live.running) return { dot: 'bg-blue-400', tip: 'Running (no data)' };
    return { dot: 'bg-gray-400', tip: 'Idle' };
  }

  // Active count = only agents that are truly contributing (running + has_data + no errors + not stale)
  const activeCount = enabledAgents.filter(a => {
    const live = liveStatuses?.[a.key];
    return live?.running && live?.has_data && !live?.stale && !(live?.errors > 0);
  }).length;

  const errorCount = enabledAgents.filter(a => {
    const live = liveStatuses?.[a.key];
    return live && live.errors > 0;
  }).length;

  // Health ratio among enabled agents
  const healthRatio = enabledAgents.length > 0 ? activeCount / enabledAgents.length : 0;
  // Card-level severity: red (< 25% healthy or errors > 50%), amber (< 60%), green (≥ 60%)
  const cardSeverity: 'red' | 'amber' | 'green' =
    healthRatio < 0.25 || errorCount > enabledAgents.length * 0.5 ? 'red'
    : healthRatio < 0.60 ? 'amber'
    : 'green';

  const borderColor = { red: 'border-red-300', amber: 'border-amber-300', green: 'border-gray-200' }[cardSeverity];
  const countColor = { red: 'text-red-600', amber: 'text-amber-600', green: 'text-gray-400' }[cardSeverity];
  const warningMsg = cardSeverity === 'red'
    ? 'Critical — intelligence severely degraded, confluence scoring weakened'
    : cardSeverity === 'amber'
    ? 'Degraded — some agents unhealthy, reduced confluence accuracy'
    : null;

  return (
    <div className={cn('bg-white rounded-xl border p-4 h-full', borderColor)}>
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-1">
        AI Agents <span className={cn('ml-1', countColor)}>{activeCount}/{enabledAgents.length} active</span>
      </p>
      {warningMsg && (
        <p className={cn('text-[10px] font-medium mb-2', cardSeverity === 'red' ? 'text-red-600' : 'text-amber-600')}>
          {warningMsg}
        </p>
      )}
      <div className="space-y-1 max-h-[280px] overflow-y-auto">
        {enabledAgents.map(a => {
          const { dot, tip } = agentDot(a);
          return (
            <div key={a.key} className="flex items-center justify-between text-sm py-0.5 group">
              <div className="flex items-center gap-2 min-w-0">
                <span className={cn('w-1.5 h-1.5 rounded-full shrink-0', dot)} />
                <span className="text-gray-900 truncate">{a.name}</span>
              </div>
              <span className="text-[10px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-2">{tip}</span>
            </div>
          );
        })}
        {disabledAgents.map(a => (
          <div key={a.key} className="flex items-center justify-between text-sm py-0.5 group">
            <div className="flex items-center gap-2 min-w-0">
              <span className="w-1.5 h-1.5 rounded-full bg-gray-300 shrink-0" />
              <span className="text-gray-400 line-through truncate">{a.name}</span>
            </div>
            <span className="text-[10px] text-gray-400 opacity-0 group-hover:opacity-100 transition-opacity shrink-0 ml-2">Disabled</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Regime config (matches MarketRegime page) ──────────────
const REGIME_STYLES: Record<string, { label: string; bg: string; text: string; dot: string }> = {
  bull_trend:           { label: 'Bull',      bg: 'bg-green-100',   text: 'text-green-700',   dot: 'bg-green-500' },
  bear_trend:           { label: 'Bear',      bg: 'bg-red-100',     text: 'text-red-700',     dot: 'bg-red-500' },
  ranging:              { label: 'Range',     bg: 'bg-yellow-100',  text: 'text-yellow-700',  dot: 'bg-yellow-500' },
  vol_expansion:        { label: 'Vol+',      bg: 'bg-purple-100',  text: 'text-purple-700',  dot: 'bg-purple-500' },
  volatility_expansion: { label: 'Vol+',      bg: 'bg-purple-100',  text: 'text-purple-700',  dot: 'bg-purple-500' },
  vol_compression:      { label: 'Vol-',      bg: 'bg-violet-100',  text: 'text-violet-700',  dot: 'bg-violet-500' },
  accumulation:         { label: 'Accum',     bg: 'bg-emerald-100', text: 'text-emerald-700', dot: 'bg-emerald-500' },
  distribution:         { label: 'Dist',      bg: 'bg-orange-100',  text: 'text-orange-700',  dot: 'bg-orange-500' },
  uncertain:            { label: 'Uncertain', bg: 'bg-gray-100',    text: 'text-gray-500',    dot: 'bg-gray-400' },
};

function ActiveRegimesCard({ regimes }: { regimes: Record<string, string> }) {
  const entries = Object.entries(regimes).sort(([a], [b]) => a.localeCompare(b));

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 h-full">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-2">
        Active Regimes <span className="text-gray-400 ml-1">{entries.length} symbols</span>
      </p>
      {entries.length === 0 ? (
        <p className="text-sm text-gray-400 py-2">No regime data yet</p>
      ) : (
        <div className="space-y-1.5 max-h-[280px] overflow-y-auto">
          {entries.map(([symbol, regime]) => {
            const cfg = REGIME_STYLES[regime] || REGIME_STYLES['uncertain'];
            return (
              <div key={symbol} className="flex items-center justify-between py-0.5">
                <span className="text-sm font-medium text-gray-900">{symbol.replace('/USDT', '')}</span>
                <span className={cn('px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider', cfg.bg, cfg.text)}>
                  {cfg.label}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

function StrategiesCard() {
  const active = ACTIVE_STRATEGIES.filter(s => s.status === 'active');
  const disabled = ACTIVE_STRATEGIES.filter(s => s.status === 'disabled');

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4 h-full">
      <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-2">
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

// ── Dashboard Reference Guide ─────────────────────────────
interface GuideEntry {
  id: string;
  title: string;
  category: string;
  definition: string;
  details: string[];
}

const GUIDE_CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  Metric: { bg: 'bg-blue-100', text: 'text-blue-700' },
  Status: { bg: 'bg-emerald-100', text: 'text-emerald-700' },
  System: { bg: 'bg-purple-100', text: 'text-purple-700' },
  Risk: { bg: 'bg-red-100', text: 'text-red-700' },
};

const DASHBOARD_GUIDE: GuideEntry[] = [
  {
    id: 'ai-agents',
    title: 'AI Agents Status',
    category: 'Status',
    definition: 'Each AI agent runs continuously to gather market intelligence — funding rates, news sentiment, on-chain flows, whale activity, and more. The status dot next to each agent reflects its real-time health from the live API.',
    details: [
      'Green — Agent is actively running, data is fresh, and no errors have been reported.',
      'Amber — Agent is running but its data is stale (hasn\'t updated within the expected refresh interval). This may indicate a temporary upstream API delay.',
      'Red — Agent has encountered one or more errors. Check the Logs page for details on the specific failure.',
      'Gray (solid) — No live data has been received yet from this agent, or the agent is idle awaiting its next scheduled run.',
      'Gray (strikethrough) — Agent is disabled in configuration. It can be re-enabled in Settings → Agents without restarting.',
      'The header shows "X/12 active" based on enabled agents that are running, fresh, and error-free — not simply enabled in config.',
      'Card border turns red when < 25% of enabled agents are healthy (or > 50% have errors), and amber when < 60% are healthy. This signals degraded intelligence that weakens the orchestrator\'s confluence vote by up to 0.10 points — enough to miss marginal trade setups.',
      'Core technical models (MomentumBreakout, PullbackLong, SwingLowContinuation) do NOT depend on agents and will continue to fire. But FundingRateModel and SentimentModel require their agents to contribute votes.',
    ],
  },
  {
    id: 'drawdown',
    title: 'Drawdown',
    category: 'Metric',
    definition: 'Peak-to-trough drawdown expressed as a percentage of peak equity. It measures how far the current portfolio value has fallen from its all-time high watermark.',
    details: [
      'Calculated as: (Peak Capital − Current Equity) / Peak Capital × 100%.',
      'Current Equity includes both free capital and the mark-to-market value of all open positions (unrealized P&L).',
      'Peak Capital is the highest equity watermark since the system started. It updates on every position close and partial close.',
      'A drawdown of 3% means the portfolio is 3% below its best point. A value of 0% means equity is at or above the all-time high.',
      'Color coding: default (< 2%), yellow (2–5%), red (> 5%). The circuit breaker activates at 10%.',
    ],
  },
  {
    id: 'avg-r',
    title: 'Avg R / Trade',
    category: 'Metric',
    definition: 'The average realized R-multiple across all closed trades. R represents risk — if you risked $100 on a trade (distance from entry to stop loss), a result of +1.5R means you made $150.',
    details: [
      'Computed from the realized R-multiple of each closed trade: (actual P&L) / (risk per trade at entry).',
      'A positive Avg R means the system is generating more reward than risk on average. The Phase 1 target is ≥ 0.10R.',
      'Color coding: green (≥ 0.10R, meeting target), yellow (0 to 0.10R, positive but below target), red (negative, losing on average).',
      'This metric is independent of position size — it normalizes every trade to its initial risk, making it the most objective measure of edge quality.',
      'Avg R can be negative even with a positive P&L if a few large-position wins mask many small-position losses.',
    ],
  },
  {
    id: 'profit-factor',
    title: 'Profit Factor',
    category: 'Metric',
    definition: 'The ratio of gross profits to gross losses across all closed trades. A profit factor above 1.0 means the system is profitable overall.',
    details: [
      'Calculated as: Total $ Won / Total $ Lost.',
      'A PF of 2.0 means the system earns $2 for every $1 it loses. The Phase 1 minimum acceptable is ≥ 1.10.',
      'Unlike Win Rate, Profit Factor accounts for the size of wins vs. losses. A system can have a low win rate but a high PF if its winners are much larger than losers.',
      'Values: 0 = no wins, 1.0 = breakeven, > 1.0 = profitable. Displayed as 0.00 when no trades have been closed.',
    ],
  },
  {
    id: 'win-rate',
    title: 'Win Rate',
    category: 'Metric',
    definition: 'The percentage of closed trades that ended with a positive P&L. Shown across all closed trades regardless of model or asset.',
    details: [
      'Calculated as: Winning Trades / Total Closed Trades × 100%.',
      'The Phase 1 acceptable portfolio win rate is ≥ 45%. Individual models have different baselines (e.g., MomentumBreakout targets 63.5%, PullbackLong targets 44.6%).',
      'Win Rate alone is not sufficient to evaluate system quality — a 90% win rate with tiny wins and rare catastrophic losses would still be a losing system. Always consider alongside Profit Factor and Avg R.',
    ],
  },
  {
    id: 'total-trades',
    title: 'Total Trades',
    category: 'Metric',
    definition: 'The total number of completed (closed) trades since the system started. This is the primary progress counter for Phase 1 demo evaluation.',
    details: [
      'Phase 1 requires 50 closed trades before formal performance assessment. The subtitle shows how many trades remain until this review milestone.',
      'Only fully closed trades count — open positions and partial closes are not included.',
      'After 50 trades: Win Rate, Profit Factor, and Avg R are statistically meaningful enough for phase advancement decisions.',
    ],
  },
  {
    id: 'crash-defense',
    title: 'Crash Defense',
    category: 'Risk',
    definition: 'A 7-component real-time crash scoring system that monitors for sudden market-wide drawdowns. It operates on a 4-tier response model with automatic protective actions.',
    details: [
      'NORMAL (score < 5.0) — All clear. No defensive measures active.',
      'DEFENSIVE (score ≥ 5.0) — Elevated risk detected. All long positions are moved to breakeven stop loss.',
      'HIGH ALERT (score ≥ 7.0) — Significant crash risk. Partial closes (50%) are executed on all long positions.',
      'EMERGENCY (score ≥ 8.0) — Severe market stress. All long positions are fully closed.',
      'SYSTEMIC (score ≥ 9.0) — Extreme systemic event. All positions (longs and shorts) are closed immediately.',
      'The score aggregates signals from price action, volume spikes, liquidation cascades, funding rate divergence, and more.',
    ],
  },
  {
    id: 'system-readiness',
    title: 'System Readiness',
    category: 'System',
    definition: 'A composite score (0–100%) reflecting whether all required system components are initialized and operating correctly. Derived from the validation engine\'s 20-point checklist.',
    details: [
      'Green (≥ 80%) — System is fully operational. All critical components are healthy.',
      'Yellow (50–79%) — Partially ready. Some non-critical components may be degraded.',
      'Red (< 50%) — Significant issues. Trading may be impaired. Check System Health for details.',
      'Components checked include: exchange connection, database, scanner, signal models, regime classifier, risk gate, position sizer, crash defense, notification channels, and more.',
    ],
  },
  {
    id: 'engine',
    title: 'Engine',
    category: 'System',
    definition: 'The core orchestration engine that coordinates the entire trading pipeline — from data fetching and regime classification through signal generation, confluence scoring, and order execution.',
    details: [
      'Status "Running" (green) means the engine loop is active, timers are aligned to candle boundaries, and the scan pipeline is executing on schedule.',
      'Status "Error" or "Warning" indicates the engine encountered an issue. Check Logs for the specific error.',
      'The engine manages two timer cycles: HTF (1-hour candle boundary) and LTF (15-minute candle boundary), both aligned to UTC.',
    ],
  },
  {
    id: 'threads',
    title: 'Threads',
    category: 'System',
    definition: 'The number of active OS threads in the NexusTrader process. This is a system resource health indicator — not related to trading threads or strategies.',
    details: [
      'Baseline at startup is approximately 51 threads. This includes the main thread, Qt event loop, database pool, HTTP server, timer threads, and agent worker threads.',
      'Thread count displayed in white means healthy (within normal range).',
      'Amber (> 75 threads) indicates potential thread leakage — background tasks or network calls may not be cleaning up properly.',
      'A steadily rising thread count across scan cycles is a warning sign that should be investigated in Logs.',
    ],
  },
  {
    id: 'scanner',
    title: 'Scanner',
    category: 'System',
    definition: 'The IDSS (Intelligent Decision Support System) scanner that periodically evaluates the watchlist for trading opportunities. It runs the full signal pipeline on each scan cycle.',
    details: [
      '"Running" (green) means the scanner is actively processing or waiting for the next candle-boundary trigger.',
      '"Stopped" (amber) means the scanner is not currently active. This may be normal between scan cycles or may indicate an error.',
      'Last Scan shows the timestamp of the most recent completed scan cycle. Scans run every 30 minutes aligned to candle boundaries.',
      'Each scan evaluates all watchlist symbols (currently 20) through regime classification, 5+ signal models, confluence scoring, and risk gating.',
    ],
  },
  {
    id: 'data-integrity',
    title: 'Data Integrity',
    category: 'System',
    definition: 'A set of automated checks that verify the consistency and correctness of the system\'s data stores — database records, position files, and trade history.',
    details: [
      'When all checks pass, a single compact bar is shown: "X/X checks passed" with a green icon.',
      'When any check fails, the section expands to show each individual check with pass/fail status and detail messages.',
      'Checks include: database connectivity, position file consistency, trade record completeness, capital reconciliation, and more.',
      'Failures here do not necessarily mean data loss — they flag inconsistencies that should be investigated before they compound.',
    ],
  },
  {
    id: 'active-regimes',
    title: 'Active Regimes',
    category: 'Status',
    definition: 'The current market regime classification for each symbol in the watchlist, as determined by the HMM + rule-based hybrid regime classifier during the most recent scan cycle.',
    details: [
      'Regimes are recalculated every scan cycle (30 minutes) per symbol using the last 200 bars of price data.',
      'Bull / Bear — Directional trend detected via ADX, EMA slope, and HMM state. These unlock trend-following signals.',
      'Range — Low ADX, price oscillating around EMA. Pullback and mean-reversion signals are prioritized.',
      'Vol+ / Vol− — Volatility expansion or compression detected. Affects position sizing and ATR-based stop/target levels.',
      'Uncertain — The classifier has low confidence. Signals in this regime are typically filtered out by the risk gate.',
      'See the Market Regime page for full per-symbol history and confidence levels.',
    ],
  },
];

function DashboardReferenceGuide() {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <BookOpen className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">Dashboard Reference Guide</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {DASHBOARD_GUIDE.map((entry) => {
          const catColor = GUIDE_CATEGORY_COLORS[entry.category] || GUIDE_CATEGORY_COLORS['System'];
          return (
            <div key={entry.id}>
              <button
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
                className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className="text-sm font-semibold text-gray-900">{entry.title}</span>
                  <span className={cn('px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider', catColor.bg, catColor.text)}>
                    {entry.category}
                  </span>
                </div>
                <ChevronDown className={cn(
                  'w-4 h-4 text-gray-400 transition-transform duration-200',
                  open === entry.id && 'rotate-180',
                )} />
              </button>

              {open === entry.id && (
                <div className="px-5 pb-5 space-y-4 animate-in fade-in duration-200">
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Definition</h4>
                    <p className="text-sm text-gray-700 leading-relaxed">{entry.definition}</p>
                  </div>
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Details</h4>
                    <ul className="space-y-1.5">
                      {entry.details.map((point, i) => (
                        <li key={i} className="flex gap-2 text-sm text-gray-700">
                          <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                          <span className="leading-relaxed">{point}</span>
                        </li>
                      ))}
                    </ul>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Dashboard Page ─────────────────────────────────────────

export default function Dashboard() {
  const queryClient = useQueryClient();
  const { subscribe, lastMessage, status } = useWSStore();
  const [refreshing, setRefreshing] = useState(false);
  const [lastRefreshed, setLastRefreshed] = useState<Date>(new Date());

  useEffect(() => {
    if (status === 'connected') { subscribe('dashboard'); subscribe('crash_defense'); }
  }, [status, subscribe]);

  // Core dashboard data
  const { data: summary } = useQuery({ queryKey: ['dashboard-summary'], queryFn: getDashboardSummary, refetchInterval: 10000 });
  const { data: crashDefense } = useQuery({ queryKey: ['crash-defense'], queryFn: getCrashDefense, refetchInterval: 15000 });
  const { data: health } = useQuery({ queryKey: ['system-health'], queryFn: getSystemHealth, refetchInterval: 30000 });
  const { data: portfolioData } = useQuery({ queryKey: ['monitor-portfolio'], queryFn: getMonitorPortfolio, refetchInterval: 30000 });
  const { data: exchangesData } = useQuery({ queryKey: ['exchanges-list'], queryFn: getExchanges, staleTime: 60000 });
  const { data: agentData } = useQuery({ queryKey: ['agent-statuses'], queryFn: getAgentStatuses, refetchInterval: 30000 });

  // Pipeline data (for regime snapshots)
  const { data: pipelineData } = useQuery({ queryKey: ['pipeline-status-dash'], queryFn: getPipelineStatus, refetchInterval: 30000, staleTime: 15000 });

  // Validation data
  const { data: healthData } = useQuery({ queryKey: ['val-health'], queryFn: getValidationHealth, refetchInterval: 30000 });
  const { data: readinessData } = useQuery({ queryKey: ['val-readiness'], queryFn: getReadiness, refetchInterval: 60000 });
  const { data: integrityData } = useQuery({ queryKey: ['val-integrity'], queryFn: getDataIntegrity, refetchInterval: 60000 });

  const dashData = lastMessage['dashboard'] || summary;
  const crashData = lastMessage['crash_defense'] || crashDefense;

  const pnl = dashData?.pnl ?? 0;
  const capital = dashData?.capital ?? 0;
  const drawdown = dashData?.drawdown ?? 0;
  const positions = dashData?.positions ?? dashData?.open_count ?? 0;
  const totalTrades = dashData?.total_trades ?? 0;
  const avgR = dashData?.avg_r ?? 0;
  const winRate = dashData?.win_rate ?? 0;
  const pf = dashData?.profit_factor ?? 0;
  const capitalDeployed = portfolioData?.portfolio?.used_margin ?? 0;
  const activeExchange = exchangesData?.find((e: any) => e.is_active);
  const exchangeName = activeExchange ? `${activeExchange.name}` : 'Not connected';
  const components = healthData?.components || {};
  const intChecks = integrityData?.checks || [];

  // Current regimes from latest pipeline snapshot
  const currentRegimes: Record<string, string> = pipelineData?.regime_snapshots?.[0]?.regimes || {};

  // Update last refreshed when dashboard data changes
  useEffect(() => { if (dashData) setLastRefreshed(new Date()); }, [dashData]);

  const handleRefresh = async () => {
    setRefreshing(true);
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['dashboard-summary'] }),
      queryClient.invalidateQueries({ queryKey: ['system-health'] }),
      queryClient.invalidateQueries({ queryKey: ['val-health'] }),
      queryClient.invalidateQueries({ queryKey: ['val-readiness'] }),
      queryClient.invalidateQueries({ queryKey: ['val-integrity'] }),
    ]);
    setLastRefreshed(new Date());
    setTimeout(() => setRefreshing(false), 1000);
  };

  return (
    <div className="space-y-5">
      {/* Header */}
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-bold text-gray-900">Dashboard</h1>
        <div className="flex items-center gap-3">
          <button
            onClick={handleRefresh}
            disabled={refreshing}
            className={cn(
              'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium min-h-[32px] transition-colors',
              refreshing ? 'bg-gray-100 text-gray-400' : 'bg-gray-50 text-gray-600 hover:bg-gray-100 border border-gray-200',
            )}
          >
            <RefreshCw className={cn('w-3.5 h-3.5', refreshing && 'animate-spin')} />
            Refresh
          </button>
          <span className="text-[11px] text-gray-400">
            {lastRefreshed.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
          </span>
          <div className="flex items-center gap-2">
            <span className={cn('w-2 h-2 rounded-full', status === 'connected' ? 'bg-green-500' : status === 'connecting' ? 'bg-yellow-500' : 'bg-gray-300')} />
            <span className="text-xs text-gray-500">
              {status === 'connected' ? 'Live' : status === 'connecting' ? 'Connecting...' : 'Offline'}
            </span>
          </div>
        </div>
      </div>

      {/* Row 1: Key Metrics */}
      <div className="grid grid-cols-2 lg:grid-cols-6 gap-3">
        <StatCard
          label="Exchange"
          value={exchangeName}
          sub={health?.exchange === 'ok' ? 'Connected' : 'Disconnected'}
          color={health?.exchange === 'ok' ? 'green' : 'red'}
        />
        <StatCard label="Capital" value={formatUSD(capital)} />
        <StatCard label="Capital Deployed" value={formatUSD(capitalDeployed)} sub={`${positions} position${positions !== 1 ? 's' : ''}`} />
        <StatCard label="PnL" value={formatUSD(pnl)} color={pnl >= 0 ? 'green' : 'red'} />
        <StatCard label="Drawdown" value={formatPct(drawdown)} color={Math.abs(drawdown) > 5 ? 'red' : Math.abs(drawdown) > 2 ? 'yellow' : 'default'} />
        <StatCard label="Total Trades" value={String(totalTrades)} sub={totalTrades < 50 ? `${50 - totalTrades} to Phase 1 review` : 'Phase 1 target met'} />
      </div>

      {/* Row 2: Performance + Defense + Readiness */}
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        <StatCard label="Win Rate" value={formatPct(winRate)} sub="All closed trades" />
        <StatCard label="Profit Factor" value={pf.toFixed(2)} sub="Gross profit / loss" />
        <StatCard
          label="Avg R / Trade"
          value={`${avgR >= 0 ? '+' : ''}${avgR.toFixed(3)}R`}
          sub="Target ≥ 0.10R"
          color={avgR >= 0.10 ? 'green' : avgR >= 0 ? 'yellow' : 'red'}
        />
        {crashData && (
          <CrashDefenseCard tier={crashData.tier || 'NORMAL'} score={crashData.score ?? 0} isDefensive={crashData.is_defensive ?? false} />
        )}
        <StatCard
          label="System Readiness"
          value={readinessData ? `${readinessData.score}%` : '--'}
          sub={readinessData?.verdict?.replace(/_/g, ' ') || 'Loading...'}
          color={readinessData ? (readinessData.score >= 80 ? 'green' : readinessData.score >= 50 ? 'yellow' : 'red') : 'default'}
        />
      </div>

      {/* Row 3: System Health + Active Regimes + Active Strategies + AI Agents */}
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
        <SystemHealthCard
          health={health}
          components={components}
          threadCount={healthData?.thread_count}
          uptimeSeconds={healthData?.uptime_s}
          scannerRunning={pipelineData?.scanner_running}
          lastScanAt={pipelineData?.last_scan_at}
        />
        <ActiveRegimesCard regimes={currentRegimes} />
        <StrategiesCard />
        <AIAgentsCard liveStatuses={agentData} />
      </div>

      {/* Row 4: Data Integrity — compact when healthy, expanded when issues found */}
      {intChecks.length > 0 && (() => {
        const allPassed = integrityData?.passed ?? true;
        const failedChecks = intChecks.filter((c: any) => c.status !== 'pass' && c.status !== 'PASS');

        return allPassed ? (
          <div className="bg-white rounded-xl border border-gray-200 px-4 py-3 flex items-center justify-between">
            <div className="flex items-center gap-2">
              <CheckCircle className="w-4 h-4 text-green-600" />
              <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">Data Integrity</p>
            </div>
            <span className="text-xs text-green-600 font-medium">{intChecks.length}/{intChecks.length} checks passed</span>
          </div>
        ) : (
          <div className="bg-white rounded-xl border border-red-200 p-4 space-y-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <XCircle className="w-4 h-4 text-red-600" />
                <p className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">Data Integrity</p>
              </div>
              <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
                {failedChecks.length} ISSUE{failedChecks.length !== 1 ? 'S' : ''} FOUND
              </span>
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {intChecks.map((c: any, i: number) => {
                const passed = c.status === 'pass' || c.status === 'PASS';
                return (
                  <div key={i} className={cn('rounded-lg border p-3', passed ? 'border-green-200 bg-green-50/50' : 'border-red-200 bg-red-50/50')}>
                    <div className="flex items-center gap-2 mb-0.5">
                      {passed ? <CheckCircle className="w-3.5 h-3.5 text-green-600" /> : <XCircle className="w-3.5 h-3.5 text-red-600" />}
                      <span className="text-sm font-medium text-gray-900">{c.name}</span>
                    </div>
                    <p className="text-xs text-gray-500 ml-5">{c.detail}</p>
                  </div>
                );
              })}
            </div>
          </div>
        );
      })()}

      {/* Row 5: Reference Guide */}
      <DashboardReferenceGuide />

    </div>
  );
}
