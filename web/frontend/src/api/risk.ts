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
  const resp = await api.get('/risk/status');
  const d = resp.data;
  const r = d.risk || d;

  // Also fetch portfolio heat from monitor endpoint for accuracy
  let heat = r.portfolio_heat_pct ?? 0;
  try {
    const portResp = await api.get('/monitor/portfolio');
    const pd = portResp.data;
    const portfolio = pd.portfolio || pd;
    if (portfolio.portfolio_heat_pct !== undefined) {
      heat = portfolio.portfolio_heat_pct;
    }
  } catch { /* use risk endpoint value */ }

  return {
    portfolio_heat_pct: heat,
    drawdown_pct: r.drawdown_pct ?? 0,
    open_positions: r.open_positions ?? 0,
    circuit_breaker_on: r.circuit_breaker_on ?? false,
    daily_loss_pct: r.daily_loss_pct ?? 0,
    crash_tier: r.crash_tier ?? 'NORMAL',
    is_defensive: r.is_defensive ?? false,
  };
}

export async function getCrashDefenseDetail(): Promise<CrashDefenseDetail> {
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

export async function triggerKillSwitch(): Promise<{ status: string; message: string }> {
  const resp = await api.post('/system/kill-switch');
  return resp.data;
}
