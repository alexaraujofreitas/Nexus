import { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  Bell, Send, Loader2, CheckCircle, XCircle, RefreshCw,
  MessageSquare, Mail, Phone, Activity, TrendingUp, AlertTriangle,
} from 'lucide-react';
import {
  getNotificationHistory,
  getNotificationStats,
  testNotificationChannel,
  testAllNotificationChannels,
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

function get(obj: Record<string, any>, path: string, def: any = ''): any {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : def), obj);
}

// ── Main Page ────────────────────────────────────────────────

export default function Notifications() {
  const queryClient = useQueryClient();
  const [testStates, setTestStates] = useState<Record<string, TestState>>({});
  const [testAllLoading, setTestAllLoading] = useState(false);

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

  // Channel status from config
  const channelEnabled = (ch: string) => get(config || {}, `notifications.${ch}.enabled`, false);
  const enabledCount = CHANNELS.filter(({ key }) => channelEnabled(key)).length;

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
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Bell className="w-5 h-5 text-blue-500" />
          <div>
            <h1 className="text-xl font-bold text-gray-900">Notifications</h1>
            <p className="text-sm text-gray-500">Channel status, delivery history, and diagnostics</p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={handleTestAll}
            disabled={testAllLoading}
            className={cn(
              'flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium min-h-[36px] transition-colors',
              testAllLoading ? 'bg-gray-100 text-gray-400' : 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100',
            )}
          >
            {testAllLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
            Test All Channels
          </button>
          <button
            onClick={handleRefresh}
            className="flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium min-h-[36px] bg-gray-50 text-gray-600 hover:bg-gray-100 border border-gray-200 transition-colors"
          >
            <RefreshCw className="w-4 h-4" /> Refresh
          </button>
        </div>
      </div>

      {/* Stats Overview */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-1">
            <Activity className="w-4 h-4 text-blue-500" />
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Channels Active</span>
          </div>
          <span className="text-2xl font-bold text-gray-900">{enabledCount}<span className="text-sm font-normal text-gray-400">/{CHANNELS.length}</span></span>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-1">
            <Send className="w-4 h-4 text-green-500" />
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Total Sent</span>
          </div>
          <span className="text-2xl font-bold text-gray-900">{totalSent.toLocaleString()}</span>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-1">
            <AlertTriangle className="w-4 h-4 text-red-500" />
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Failed</span>
          </div>
          <span className={cn('text-2xl font-bold', totalFailed > 0 ? 'text-red-600' : 'text-gray-900')}>{totalFailed.toLocaleString()}</span>
        </div>
        <div className="bg-white rounded-lg border border-gray-200 p-4">
          <div className="flex items-center gap-2 mb-1">
            <TrendingUp className="w-4 h-4 text-emerald-500" />
            <span className="text-xs font-medium text-gray-500 uppercase tracking-wide">Success Rate</span>
          </div>
          <span className={cn(
            'text-2xl font-bold',
            successRate >= 0.95 ? 'text-green-600' : successRate >= 0.8 ? 'text-amber-600' : 'text-red-600',
          )}>
            {(successRate * 100).toFixed(1)}%
          </span>
        </div>
      </div>

      {/* Channel Status Cards */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Delivery Channels</h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          {CHANNELS.map(({ key, label, icon: Icon }) => {
            const enabled = channelEnabled(key);
            const st = testStates[key] || 'idle';
            return (
              <div key={key} className={cn(
                'bg-white rounded-lg border p-4 transition-colors',
                enabled ? 'border-gray-200' : 'border-gray-100',
              )}>
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <Icon className={cn('w-4 h-4', enabled ? 'text-gray-700' : 'text-gray-300')} />
                    <span className={cn('text-sm font-semibold', enabled ? 'text-gray-900' : 'text-gray-400')}>{label}</span>
                  </div>
                  <div className="flex items-center gap-1.5">
                    <span className={cn('w-2 h-2 rounded-full', enabled ? 'bg-green-500' : 'bg-gray-300')} />
                    <span className={cn('text-xs', enabled ? 'text-green-700' : 'text-gray-400')}>
                      {enabled ? 'Active' : 'Disabled'}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => handleTest(key)}
                  disabled={st === 'testing' || !enabled}
                  className={cn(
                    'w-full flex items-center justify-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium min-h-[34px] transition-colors',
                    !enabled ? 'bg-gray-50 text-gray-300 cursor-not-allowed' :
                    st === 'success' ? 'bg-green-100 text-green-700' :
                    st === 'failed' ? 'bg-red-100 text-red-700' :
                    st === 'testing' ? 'bg-gray-100 text-gray-400' :
                    'bg-blue-50 text-blue-700 hover:bg-blue-100',
                  )}
                >
                  {st === 'testing' && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
                  {st === 'success' && <CheckCircle className="w-3.5 h-3.5" />}
                  {st === 'failed' && <XCircle className="w-3.5 h-3.5" />}
                  {st === 'idle' && <Send className="w-3.5 h-3.5" />}
                  {st === 'success' ? 'Delivered' : st === 'failed' ? 'Failed' : st === 'testing' ? 'Sending...' : 'Send Test'}
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Notification History */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider">Delivery History</h2>
          <span className="text-xs text-gray-400">{notifications.length} notification{notifications.length !== 1 ? 's' : ''}</span>
        </div>
        <div className="bg-white rounded-lg border border-gray-200">
          <div className="overflow-auto max-h-[480px]">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-gray-50 z-10">
                <tr>
                  <th className="text-left px-4 py-2.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Time</th>
                  <th className="text-left px-4 py-2.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Type</th>
                  <th className="text-left px-4 py-2.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Dedup Key</th>
                  <th className="text-left px-4 py-2.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Channels</th>
                  <th className="text-left px-4 py-2.5 text-[11px] font-semibold text-gray-500 uppercase tracking-wider">Status</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-50">
                {historyLoading ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-gray-400">
                      <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
                      Loading history...
                    </td>
                  </tr>
                ) : notifications.length === 0 ? (
                  <tr>
                    <td colSpan={5} className="px-4 py-12 text-center text-gray-400">
                      <Bell className="w-6 h-6 mx-auto mb-2 opacity-40" />
                      <p className="text-sm">No notifications sent yet</p>
                      <p className="text-xs mt-1">Notifications will appear here once the system starts sending alerts</p>
                    </td>
                  </tr>
                ) : (
                  notifications.map((n: NotificationHistoryEntry, i: number) => {
                    const time = n.sent_at ? new Date(n.sent_at).toLocaleString(undefined, {
                      month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit',
                    }) : '--';
                    const type = n.template.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase());
                    return (
                      <tr key={i} className={cn('transition-colors hover:bg-gray-50/80', i % 2 === 0 ? 'bg-white' : 'bg-gray-50/30')}>
                        <td className="px-4 py-2.5 text-gray-500 font-mono text-xs whitespace-nowrap">{time}</td>
                        <td className="px-4 py-2.5 text-gray-800 font-medium text-xs">{type}</td>
                        <td className="px-4 py-2.5 text-gray-400 font-mono text-xs truncate max-w-[180px]">{n.dedup_key || '--'}</td>
                        <td className="px-4 py-2.5">
                          <div className="flex flex-wrap gap-1">
                            {(n.channels || []).length > 0 ? n.channels.map((ch) => (
                              <span key={ch} className="inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-blue-50 text-blue-600">
                                {ch}
                              </span>
                            )) : <span className="text-xs text-gray-300">--</span>}
                          </div>
                        </td>
                        <td className="px-4 py-2.5">
                          {n.success ? (
                            <span className="inline-flex items-center gap-1 text-xs font-medium text-green-700">
                              <CheckCircle className="w-3.5 h-3.5" /> Delivered
                            </span>
                          ) : (
                            <span className="inline-flex items-center gap-1 text-xs font-medium text-red-600">
                              <XCircle className="w-3.5 h-3.5" /> Failed
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

      {/* Footer hint */}
      <div className="text-xs text-gray-400 text-center">
        Channel configuration and notification preferences are managed in <span className="font-medium text-gray-500">Settings &rarr; Notifications</span>
      </div>
    </div>
  );
}
