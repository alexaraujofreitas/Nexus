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
  const resp = await api.get('/dashboard/summary');
  const d = resp.data;
  const p = d.portfolio || {};
  const cd = d.crash_defense || {};
  return {
    capital: p.capital_usdt ?? 0,
    pnl: p.session_pnl_usdt ?? p.total_pnl_usdt ?? 0,
    drawdown: p.drawdown_pct ?? 0,
    positions: p.open_positions ?? 0,
    crash_tier: cd.tier ?? 'NORMAL',
    recent_trades: d.recent_trades || [],
    win_rate: p.win_rate ?? 0,
    profit_factor: p.profit_factor ?? 0,
  };
}

export async function getCrashDefense(): Promise<CrashDefenseStatus> {
  const resp = await api.get('/dashboard/crash-defense');
  const d = resp.data;
  const cd = d.crash_defense || d;
  return {
    tier: cd.tier ?? 'NORMAL',
    score: cd.score ?? 0,
    is_defensive: cd.is_defensive ?? false,
    actions_log: cd.actions_log || [],
  };
}

export async function getSystemHealth(): Promise<SystemHealth> {
  const resp = await api.get('/system/health');
  const d = resp.data;
  const c = d.components || {};
  return {
    exchange: c.exchange?.initialized ? 'ok' : 'error',
    database: 'ok',
    threads: c.threads?.count ?? 0,
    scanner: c.scanner?.running ? 'running' : 'stopped',
    agents: 'ok',
    uptime: c.engine?.uptime_s ?? 0,
  };
}
