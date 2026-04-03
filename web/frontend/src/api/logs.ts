import api from './client';

export interface LogEntry {
  timestamp: string;
  level: string;
  component: string;
  message: string;
  extra?: Record<string, unknown>;
}

export interface LogsResponse {
  entries: LogEntry[];
  count: number;
}

export async function getRecentLogs(params: {
  limit?: number;
  level?: string;
  component?: string;
  search?: string;
} = {}): Promise<LogsResponse> {
  const resp = await api.get<LogsResponse>('/logs/recent', { params });
  return resp.data;
}
