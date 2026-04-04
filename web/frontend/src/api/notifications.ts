import api from './client';

// ── Types ────────────────────────────────────────────────────

export type NotificationChannel = 'whatsapp' | 'telegram' | 'email' | 'gemini' | 'sms';

export interface NotificationHistoryEntry {
  template: string;
  dedup_key: string;
  sent_at: string;
  success: boolean;
  channels: string[];
}

export interface NotificationStats {
  total_sent: number;
  total_failed: number;
  total_retried: number;
  success_rate: number;
}

export interface NotificationHistory {
  notifications: NotificationHistoryEntry[];
  stats: NotificationStats;
}

export interface TestChannelResult {
  status: string;
  channel?: string;
  message?: string;
  success?: boolean;
}

export interface TestAllResult {
  [channel: string]: boolean;
}

// ── API Functions ────────────────────────────────────────────

/** Test a single notification channel */
export async function testNotificationChannel(
  channel: NotificationChannel,
): Promise<TestChannelResult> {
  const resp = await api.post(`/settings/notifications/test/${channel}`);
  return resp.data;
}

/** Test all configured notification channels */
export async function testAllNotificationChannels(): Promise<TestAllResult> {
  const resp = await api.post('/settings/notifications/test-all');
  return resp.data;
}

/** Get notification delivery history */
export async function getNotificationHistory(
  limit: number = 50,
): Promise<NotificationHistory> {
  const resp = await api.get('/settings/notifications/history', {
    params: { limit },
  });
  return resp.data;
}

/** Get notification delivery statistics */
export async function getNotificationStats(): Promise<NotificationStats> {
  const resp = await api.get('/settings/notifications/stats');
  return resp.data;
}

/** Update notification preferences (which types are enabled) */
export async function updateNotificationPreferences(
  preferences: Record<string, boolean>,
): Promise<{ status: string }> {
  const resp = await api.put('/settings/notifications/preferences', {
    preferences,
  });
  return resp.data;
}

/** Set health check interval in hours */
export async function setHealthCheckInterval(
  hours: number,
): Promise<{ status: string }> {
  const resp = await api.put('/settings/notifications/health-check-interval', {
    hours,
  });
  return resp.data;
}
