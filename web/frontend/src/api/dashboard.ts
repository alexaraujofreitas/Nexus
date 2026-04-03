import api from './client';

export interface DashboardSummary {
  capital: number;
  pnl: number;
  drawdown: number;
  positions: number;
  crash_tier: string;
  recent_trades: Array<{
    symbol: string;
    side: string;
    pnl_usdt: number;
    closed_at: string;
  }>;
  win_rate: number;
  profit_factor: number;
}

export interface CrashDefenseStatus {
  tier: string;
  score: number;
  is_defensive: boolean;
  actions_log: string[];
}

export interface SystemHealth {
  exchange: string;
  database: string;
  threads: number;
  scanner: string;
  agents: string;
  uptime: number;
}

export async function getDashboardSummary(): Promise<DashboardSummary> {
  const resp = await api.get<DashboardSummary>('/dashboard/summary');
  return resp.data;
}

export async function getCrashDefense(): Promise<CrashDefenseStatus> {
  const resp = await api.get<CrashDefenseStatus>('/dashboard/crash-defense');
  return resp.data;
}

export async function getSystemHealth(): Promise<SystemHealth> {
  const resp = await api.get<SystemHealth>('/system/health');
  return resp.data;
}
