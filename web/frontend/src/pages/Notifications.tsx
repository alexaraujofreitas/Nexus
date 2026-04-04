import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bell, Send, Loader2, CheckCircle, XCircle, RefreshCw,
  MessageSquare, Mail, Phone,
} from 'lucide-react';
import {
  getNotificationHistory,
  getNotificationStats,
  testNotificationChannel,
  testAllNotificationChannels,
  updateNotificationPreferences,
  setHealthCheckInterval,
  type NotificationChannel,
  type NotificationHistoryEntry,
} from '../api/notifications';
import { getSettings } from '../api/settings';
import { cn } from '../lib/utils';

// ── Types ────────────────────────────────────────────────────

type TestState = 'idle' | 'testing' | 'success' | 'failed';

const CHANNELS: { key: NotificationChannel; label: string; icon: typeof MessageSquare }[] = [
  { key: 'whatsapp', label: 'WhatsApp', icon: MessageSquare },
  { key: 'telegram', label: 'Telegram', icon: Send },
  { key: 'email', label: 'Email', icon: Mail },
  { key: 'sms', label: 'SMS', icon: Phone },
];

const PREFERENCE_KEYS: { key: string; label: string; defaultOn: boolean }[] = [
  { key: 'trade_opened', label: 'Trade Opened', defaultOn: true },
  { key: 'trade_closed', label: 'Trade Closed', defaultOn: true },
  { key: 'trade_stopped', label: 'Stop-Loss Hit', defaultOn: true },
  { key: 'trade_rejected', label: 'Signal Rejected', defaultOn: false },
  { key: 'trade_modified', label: 'Trade Modified', defaultOn: false },
  { key: 'strategy_signal', label: 'Strategy Signal', defaultOn: false },
  { key: 'risk_warning', label: 'Risk Warning', defaultOn: true },
  { key: 'market_condition', label: 'Market / Regime Alert', defaultOn: false },
  { key: 'system_error', label: 'System Errors', defaultOn: true },
  { key: 'emergency_stop', label: 'Emergency Stop', defaultOn: true },
  { key: 'daily_summary', label: 'Daily Summary', defaultOn: true },
];

const HEALTH_INTERVALS = [1, 2, 3, 4, 6, 12, 24];

function get(obj: Record<string, any>, path: string, def: any = ''): any {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : def), obj);
}

// ── Main Page ────────────────────────────────────────────────

export default function Notifications() {
  const queryClient = useQueryClient();
  const [testStates, setTestStates] = useState<Record<string, TestState>>({});
  const [testAllLoading, setTestAllLoading] = useState(false);
  const [prefSaving, setPrefSaving] = useState(false);
  const [prefSaved, setPrefSaved] = useState(false);

  // Data queries
  const { data: history, isLoading: historyLoading } = useQuery({
    queryKey: ['notification-history'],
    queryFn: () => getNotificationHistory(100),
    refetchInterval: 30000,
  });

  const { data: stats } = useQuery({
    queryKey: ['notification-stats'],
    queryFn: () => getNotificationStats(),
    refetchInterval: 30000,
  });

  const { data: config } = useQuery({
    queryKey: ['settings'],
    queryFn: () => getSettings(),
    refetchInterval: 60000,
  });

  // Local preference state
  const [prefs, setPrefs] = useState<Record<string, boolean>>({});
  const [healthCheck, setHealthCheck] = useState(true);
  const [healthInterval, setHealthInterval] = useState(6);

  useEffect(() => {
    if (config) {
      const p: Record<string, boolean> = {};
      for (const { key, defaultOn } of PREFERENCE_KEYS) {
        p[key] = get(config, `notifications.preferences.${key}`, defaultOn);
      }
      setPrefs(p);
      setHealthCheck(get(config, 'notifications.preferences.health_check', true));
      setHealthInterval(get(config, 'notifications.preferences.health_check_interval_hours', 6));
    }
  }, [config]);

  // Channel status from config
  const channelEnabled = (ch: string) => get(config || {}, `notifications.${ch}.enabled`, false);

  // Test handlers
  const handleTest = async (channel: NotificationChannel) => {
    setTestStates((p) => ({ ...p, [channel]: 'testing' }));
    try {
      await testNotificationChannel(channel);
      setTestStates((p) => ({ ...p, [channel]: 'success' }));
    } catch {
      setTestStates((p) => ({ ...p, [channel]: 'failed' }));
    }
    setTimeout(() => setTestStates((p) => ({ ...p, [channel]: 'idle' })), 5000);
    queryClient.invalidateQueries({ queryKey: ['notification-history'] });
  };

  const handleTestAll = async () => {
    setTestAllLoading(true);
    try {
      const result = await testAllNotificationChannels();
      const updated: Record<string, TestState> = {};
      for (const [ch, ok] of Object.entries(result)) {
        updated[ch] = ok ? 'success' : 'failed';
      }
      setTestStates((p) => ({ ...p, ...updated }));
      setTimeout(() => {
        setTestStates((p) => {
          const reset = { ...p };
          for (const ch of Object.keys(updated)) reset[ch] = 'idle';
          return reset;
        });
      }, 5000);
    } catch {
      // silently handle
    } finally {
      setTestAllLoading(false);
      queryClient.invalidateQueries({ queryKey: ['notification-history'] });
    }
  };

  // Save preferences
  const handleSavePrefs = async () => {
    setPrefSaving(true);
    try {
      await updateNotificationPreferences({ ...prefs, health_check: healthCheck });
      await setHealthCheckInterval(healthInterval);
      setPrefSaved(true);
      setTimeout(() => setPrefSaved(false), 2000);
    } catch {
      // silently handle
    } finally {
      setPrefSaving(false);
    }
  };

  const handleRefresh = () => {
    queryClient.invalidateQueries({ queryKey: ['notification-history'] });
    queryClient.invalidateQueries({ queryKey: ['notification-stats'] });
  };

  const notifications = history?.notifications ?? [];
  const totalSent = stats?.total_sent ?? 0;
  const totalFailed = stats?.total_failed ?? 0;
  const successRate = stats?.success_rate ?? 0;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Bell className="w-5 h-5 text-gray-400" />
        <div>
          <h1 className="text-xl font-semibold text-gray-900">Notifications</h1>
          <p className="text-sm text-gray-500">Real-time trade and system notifications — history, channels, preferences</p>
        </div>
      </div>

      {/* Channel Status Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        {CHANNELS.map(({ key, label, icon: Icon }) => {
          const enabled = channelEnabled(key);
          const st = testStates[key] || 'idle';
          return (
            <div key={key} className="bg-white rounded-lg border border-gray-200 p-4">
              <div className="flex items-center gap-2 mb-2">
                <Icon className="w-4 h-4 text-gray-400" />
                <span className="text-sm font-medium text-gray-700">{label}</span>
              </div>
              <div className="flex items-center gap-2">
                <span className={cn('w-2 h-2 rounded-full', enabled ? 'bg-green-500' : 'bg-gray-300')} />
                <span className={cn('text-xs', enabled ? 'text-green-700' : 'text-gray-400')}>
                  {enabled ? 'Enabled' : 'Disabled'}
                </span>
              </div>
              <button
                onClick={() => handleTest(key)}
                disabled={st === 'testing' || !enabled}
                className={cn(
                  'mt-3 w-full flex items-center justify-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium min-h-[32px] transition-colors',
                  !enabled ? 'bg-gray-50 text-gray-300 cursor-not-allowed' :
                  st === 'success' ? 'bg-green-100 text-green-700' :
                  st === 'failed' ? 'bg-red-100 text-red-700' :
                  st === 'testing' ? 'bg-gray-100 text-gray-400' :
                  'bg-blue-50 text-blue-700 hover:bg-blue-100',
                )}
              >
                {st === 'testing' && <Loader2 className="w-3 h-3 animate-spin" />}
                {st === 'success' && <CheckCircle className="w-3 h-3" />}
                {st === 'failed' && <XCircle className="w-3 h-3" />}
                {st === 'idle' && <Send className="w-3 h-3" />}
                {st === 'success' ? 'Sent' : st === 'failed' ? 'Failed' : st === 'testing' ? 'Sending...' : 'Test'}
              </button>
            </div>
          );
        })}
      </div>

      {/* Stats Bar */}
      <div className="flex items-center gap-6 bg-white rounded-lg border border-gray-200 px-4 py-3">
        <div className="text-sm">
          <span className="text-gray-500">Total Sent:</span>
          <span className="ml-1 font-medium text-gray-900">{totalSent}</span>
        </div>
        <div className="text-sm">
          <span className="text-gray-500">Failed:</span>
          <span className={cn('ml-1 font-medium', totalFailed > 0 ? 'text-red-600' : 'text-gray-900')}>{totalFailed}</span>
        </div>
        <div className="text-sm">
          <span className="text-gray-500">Success Rate:</span>
          <span className={cn('ml-1 font-medium', successRate >= 0.95 ? 'text-green-600' : successRate >= 0.8 ? 'text-yellow-600' : 'text-red-600')}>
            {(successRate * 100).toFixed(1)}%
          </span>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={handleTestAll}
            disabled={testAllLoading}
            className={cn(
              'flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium min-h-[32px] transition-colors',
              testAllLoading ? 'bg-gray-100 text-gray-400' : 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100',
            )}
          >
            {testAllLoading ? <Loader2 className="w-3 h-3 animate-spin" /> : <Send className="w-3 h-3" />}
            Test All
          </button>
          <button
            onClick={handleRefresh}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded text-xs font-medium min-h-[32px] bg-gray-50 text-gray-600 hover:bg-gray-100 transition-colors"
          >
            <RefreshCw className="w-3 h-3" /> Refresh
          </button>
        </div>
      </div>

      {/* Two-column: Preferences + History */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Preferences (1/3 width) */}
        <div className="bg-white rounded-lg border border-gray-200 p-4 space-y-3">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Preferences</h2>
          <div className="space-y-1">
            {PREFERENCE_KEYS.map(({ key, label }) => (
              <label key={key} className="flex items-center justify-between cursor-pointer py-1.5">
                <span className="text-sm text-gray-700">{label}</span>
                <button
                  type="button"
                  onClick={() => setPrefs((p) => ({ ...p, [key]: !p[key] }))}
                  className={cn('relative w-9 h-5 rounded-full transition-colors', prefs[key] ? 'bg-blue-600' : 'bg-gray-300')}
                >
                  <span className={cn('absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform', prefs[key] && 'translate-x-4')} />
                </button>
              </label>
            ))}
          </div>

          <div className="pt-2 border-t border-gray-100">
            <label className="flex items-center justify-between cursor-pointer py-1.5">
              <span className="text-sm text-gray-700">Health Check</span>
              <button
                type="button"
                onClick={() => setHealthCheck(!healthCheck)}
                className={cn('relative w-9 h-5 rounded-full transition-colors', healthCheck ? 'bg-blue-600' : 'bg-gray-300')}
              >
                <span className={cn('absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform', healthCheck && 'translate-x-4')} />
              </button>
            </label>
            <div className="flex items-center gap-2 mt-1">
              <span className="text-xs text-gray-500">Every</span>
              <select
                value={healthInterval}
                onChange={(e) => setHealthInterval(parseInt(e.target.value, 10))}
                className="text-xs border border-gray-300 rounded px-2 py-1 min-h-[28px] focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                {HEALTH_INTERVALS.map((h) => (
                  <option key={h} value={h}>{h}h</option>
                ))}
              </select>
            </div>
          </div>

          <button
            onClick={handleSavePrefs}
            disabled={prefSaving}
            className={cn(
              'w-full flex items-center justify-center gap-2 px-4 py-2 rounded-lg text-sm font-medium min-h-[40px] transition-colors',
              prefSaved ? 'bg-green-600 text-white' : 'bg-blue-600 text-white hover:bg-blue-700',
            )}
          >
            {prefSaved ? <><CheckCircle className="w-4 h-4" /> Saved</> : 'Save Preferences'}
          </button>
        </div>

        {/* History Table (2/3 width) */}
        <div className="lg:col-span-2 bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Notification History</h2>
            <span className="text-xs text-gray-400">{notifications.length} notification(s)</span>
          </div>

          <div className="overflow-auto max-h-[400px]">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-gray-50">
                <tr>
                  <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Time</th>
                  <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Type</th>
                  <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Key</th>
                  <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Channels</th>
                  <th className="text-left px-3 py-2 text-xs font-medium text-gray-500 uppercase">Status</th>
                </tr>
              </thead>
              <tbody>
                {historyLoading ? (
                  <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-400">Loading...</td></tr>
                ) : notifications.length === 0 ? (
                  <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-400">No notifications yet</td></tr>
                ) : (
                  notifications.map((n: NotificationHistoryEntry, i: number) => {
                    const time = n.sent_at ? new Date(n.sent_at).toLocaleTimeString() : '—';
                    const type = n.template.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
                    return (
                      <tr key={i} className={cn('border-t border-gray-50', i % 2 === 0 ? 'bg-white' : 'bg-gray-50/50')}>
                        <td className="px-3 py-2 text-gray-500 font-mono text-xs">{time}</td>
                        <td className="px-3 py-2 text-gray-700">{type}</td>
                        <td className="px-3 py-2 text-gray-400 text-xs">{n.dedup_key || '—'}</td>
                        <td className="px-3 py-2 text-gray-500 text-xs">{(n.channels || []).join(', ') || '—'}</td>
                        <td className="px-3 py-2">
                          {n.success ? (
                            <span className="inline-flex items-center gap-1 text-green-600 text-xs">
                              <CheckCircle className="w-3 h-3" /> Sent
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-red-600 text-xs">
                              <XCircle className="w-3 h-3" /> Failed
                            </span>
                          )}
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
