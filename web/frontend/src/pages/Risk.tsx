import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ShieldAlert, AlertTriangle, Power } from 'lucide-react';
import { getRiskStatus, getCrashDefenseDetail, triggerKillSwitch } from '../api/risk';
import { getPositions } from '../api/trading';
import { useWSStore } from '../stores/wsStore';
import { cn } from '../lib/utils';

// ── Crash tier colors ───────────────────────────────────────
const TIER_COLORS: Record<string, string> = {
  NORMAL: 'bg-green-100 text-green-700',
  DEFENSIVE: 'bg-yellow-100 text-yellow-700',
  HIGH_ALERT: 'bg-orange-100 text-orange-700',
  EMERGENCY: 'bg-red-100 text-red-700',
  SYSTEMIC: 'bg-red-200 text-red-800',
};

// ── Heat gauge ──────────────────────────────────────────────
function HeatGauge({ value, label }: { value: number; label: string }) {
  const color =
    value > 60 ? 'bg-red-500' : value > 30 ? 'bg-yellow-500' : 'bg-green-500';

  return (
    <div>
      <div className="flex items-center justify-between text-xs mb-1">
        <span className="text-gray-400">{label}</span>
        <span className="font-mono text-gray-700">{value.toFixed(1)}%</span>
      </div>
      <div className="w-full bg-gray-100 rounded-full h-3">
        <div
          className={cn('h-3 rounded-full transition-all', color)}
          style={{ width: `${Math.min(value, 100)}%` }}
        />
      </div>
    </div>
  );
}

// ── Typed Kill Switch Modal ─────────────────────────────────
function KillSwitchModal({
  open,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [input, setInput] = useState('');
  const canConfirm = input === 'KILL';

  useEffect(() => {
    if (!open) setInput('');
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40">
      <div className="bg-white rounded-lg shadow-xl p-6 max-w-sm w-full mx-4">
        <div className="flex items-center gap-2 mb-3">
          <AlertTriangle className="w-5 h-5 text-red-600" />
          <h3 className="font-semibold text-red-900">Emergency Kill Switch</h3>
        </div>
        <p className="text-sm text-gray-600 mb-4">
          This will close all positions, pause trading, and stop the scanner.
        </p>
        <div className="mb-4">
          <label className="block text-xs text-gray-500 mb-1">
            Type <span className="font-mono font-bold text-red-600">KILL</span> to confirm
          </label>
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono focus:outline-none focus:ring-2 focus:ring-red-500 focus:border-red-500 min-h-[44px]"
            placeholder="Type KILL"
            autoFocus
          />
        </div>
        <div className="flex gap-3 justify-end">
          <button
            onClick={onCancel}
            className="px-4 py-2 rounded-lg text-sm font-medium text-gray-600 hover:bg-gray-100 min-h-[44px]"
          >
            Cancel
          </button>
          <button
            onClick={() => {
              if (canConfirm) onConfirm();
            }}
            disabled={!canConfirm}
            className={cn(
              'px-4 py-2 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
              canConfirm
                ? 'bg-red-600 text-white hover:bg-red-700'
                : 'bg-gray-200 text-gray-400 cursor-not-allowed',
            )}
          >
            Confirm Kill Switch
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Risk Page ───────────────────────────────────────────────
export default function Risk() {
  const [killModalOpen, setKillModalOpen] = useState(false);
  const [killResult, setKillResult] = useState<string | null>(null);

  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (status === 'connected') {
      subscribe('risk');
      subscribe('crash_defense');
    }
  }, [status, subscribe]);

  // WS updates
  useEffect(() => {
    const wsRisk = lastMessage['risk'];
    if (wsRisk) queryClient.setQueryData(['risk-status'], wsRisk);
  }, [lastMessage, queryClient]);

  useEffect(() => {
    const wsCrash = lastMessage['crash_defense'];
    if (wsCrash) queryClient.setQueryData(['crash-defense-detail'], wsCrash);
  }, [lastMessage, queryClient]);

  // API queries
  const { data: riskData } = useQuery({
    queryKey: ['risk-status'],
    queryFn: getRiskStatus,
    refetchInterval: 10000,
  });

  const { data: crashData } = useQuery({
    queryKey: ['crash-defense-detail'],
    queryFn: getCrashDefenseDetail,
    refetchInterval: 15000,
  });

  const { data: posData } = useQuery({
    queryKey: ['trading-positions'],
    queryFn: getPositions,
    refetchInterval: 15000,
  });

  const handleKillSwitch = async () => {
    setKillModalOpen(false);
    try {
      const result = await triggerKillSwitch();
      setKillResult(result.message || 'Kill switch activated');
      queryClient.invalidateQueries({ queryKey: ['risk-status'] });
      queryClient.invalidateQueries({ queryKey: ['trading-positions'] });
    } catch {
      setKillResult('Kill switch failed — check engine status');
    }
    setTimeout(() => setKillResult(null), 5000);
  };

  const heat = riskData?.portfolio_heat_pct ?? 0;
  const drawdown = riskData?.drawdown_pct ?? 0;
  const dailyLoss = riskData?.daily_loss_pct ?? 0;
  const circuitBreaker = riskData?.circuit_breaker_on ?? false;
  const crashTier = crashData?.tier || riskData?.crash_tier || 'NORMAL';
  const crashScore = crashData?.score ?? 0;
  const isDefensive = crashData?.is_defensive ?? riskData?.is_defensive ?? false;
  const actionsLog = crashData?.actions_log || [];
  const positions = posData?.positions || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <ShieldAlert className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Risk Management</h1>
        <span
          className={cn(
            'px-2 py-0.5 rounded text-xs font-medium',
            TIER_COLORS[crashTier] || 'bg-gray-100 text-gray-600',
          )}
        >
          {crashTier}
        </span>
      </div>

      {/* Kill result toast */}
      {killResult && (
        <div className="bg-red-50 border border-red-200 text-red-800 text-sm rounded-lg px-4 py-3">
          {killResult}
        </div>
      )}

      {/* Circuit breaker alert */}
      {circuitBreaker && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 text-sm rounded-lg px-4 py-3 flex items-center gap-2">
          <AlertTriangle className="w-4 h-4" />
          Circuit breaker is active — trading paused
        </div>
      )}

      {/* Main grid */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Portfolio Heat Card */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Portfolio Heat</p>
          <HeatGauge value={heat} label="Portfolio Heat" />
          <HeatGauge value={Math.abs(drawdown)} label="Drawdown" />
          <HeatGauge value={Math.abs(dailyLoss)} label="Daily Loss" />
          <div className="flex items-center justify-between text-sm pt-2 border-t border-gray-100">
            <span className="text-gray-500">Circuit Breaker</span>
            <span className={circuitBreaker ? 'text-red-600 font-medium' : 'text-green-600'}>
              {circuitBreaker ? 'ON' : 'OFF'}
            </span>
          </div>
        </div>

        {/* Crash Defense Card */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Crash Defense</p>
          <div className="flex items-center gap-3">
            <span
              className={cn(
                'px-3 py-1.5 rounded-lg text-sm font-bold',
                TIER_COLORS[crashTier] || 'bg-gray-100 text-gray-600',
              )}
            >
              {crashTier}
            </span>
            <span className="text-sm text-gray-600">
              Score: <span className="font-mono">{crashScore.toFixed(1)}</span>
            </span>
            {isDefensive && (
              <span className="text-xs text-yellow-600 font-medium">Defensive mode active</span>
            )}
          </div>

          {/* Actions log */}
          <div>
            <p className="text-xs text-gray-400 mb-2">Recent Actions</p>
            <div className="max-h-40 overflow-y-auto space-y-1">
              {actionsLog.length === 0 ? (
                <p className="text-xs text-gray-400">No defensive actions</p>
              ) : (
                actionsLog.map((entry, i) => (
                  <div key={i} className="text-xs text-gray-600 flex gap-2">
                    <span className="text-gray-400 shrink-0">
                      {typeof entry === 'string' ? '' : entry.timestamp}
                    </span>
                    <span>{typeof entry === 'string' ? entry : entry.action}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        {/* Controls Card */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-4">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Controls</p>
          <button
            onClick={() => setKillModalOpen(true)}
            className="w-full flex items-center justify-center gap-2 px-6 py-3 rounded-lg text-sm font-bold bg-red-600 text-white hover:bg-red-700 active:bg-red-800 transition-colors min-h-[48px]"
          >
            <Power className="w-5 h-5" />
            Emergency Kill Switch
          </button>
          <p className="text-xs text-gray-400 text-center">
            Closes all positions, pauses trading, stops scanner
          </p>
        </div>

        {/* Open Exposure Card */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide">Open Exposure</p>
          {positions.length === 0 ? (
            <p className="text-sm text-gray-400">No open positions</p>
          ) : (
            <div className="space-y-2">
              {positions.map((pos) => (
                <div key={pos.symbol} className="flex items-center justify-between text-sm">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900">{pos.symbol}</span>
                    <span
                      className={cn(
                        'text-xs',
                        pos.side === 'buy' || pos.side === 'long' ? 'text-green-600' : 'text-red-600',
                      )}
                    >
                      {pos.side === 'buy' || pos.side === 'long' ? 'LONG' : 'SHORT'}
                    </span>
                  </div>
                  <span className="font-mono text-gray-700">
                    ${(pos.size_usdt ?? 0).toFixed(0)}
                  </span>
                </div>
              ))}
              <div className="pt-2 border-t border-gray-100 flex justify-between text-sm">
                <span className="text-gray-500">Total</span>
                <span className="font-mono font-medium text-gray-900">
                  ${positions.reduce((s, p) => s + (p.size_usdt ?? 0), 0).toFixed(0)}
                </span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Kill Switch Modal */}
      <KillSwitchModal
        open={killModalOpen}
        onConfirm={handleKillSwitch}
        onCancel={() => setKillModalOpen(false)}
      />
    </div>
  );
}
