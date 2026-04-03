import api from './client';

export interface RiskStatus {
  portfolio_heat_pct: number;
  drawdown_pct: number;
  open_positions: number;
  circuit_breaker_on: boolean;
  daily_loss_pct: number;
  crash_tier: string;
  is_defensive: boolean;
}

export interface CrashDefenseDetail {
  tier: string;
  score: number;
  is_defensive: boolean;
  actions_log: Array<{ timestamp: string; action: string }>;
}

export async function getRiskStatus(): Promise<RiskStatus> {
  const resp = await api.get<RiskStatus>('/risk/status');
  return resp.data;
}

export async function getCrashDefenseDetail(): Promise<CrashDefenseDetail> {
  const resp = await api.get<CrashDefenseDetail>('/dashboard/crash-defense');
  return resp.data;
}

export async function triggerKillSwitch(): Promise<{ status: string; message: string }> {
  const resp = await api.post('/system/kill-switch');
  return resp.data;
}
