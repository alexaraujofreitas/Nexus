import api from './client';

export interface ComponentHealth {
  status: string;
  detail?: string;
  error_count?: number;
  last_error?: string;
}

export interface HealthReport {
  components: Record<string, ComponentHealth>;
  thread_count?: number;
  uptime_s?: number;
}

export interface ReadinessCheck {
  name: string;
  passed: boolean;
  value: number | string;
  threshold: number | string;
  note?: string;
}

export interface ReadinessReport {
  verdict: string;
  score: number;
  checks: ReadinessCheck[];
}

export interface IntegrityCheck {
  name: string;
  status: string;
  detail: string;
}

export interface IntegrityReport {
  passed: boolean;
  checks: IntegrityCheck[];
}

export async function getValidationHealth(): Promise<HealthReport> {
  const resp = await api.get('/validation/health');
  const d = resp.data;
  const raw = d.components || {};
  // Transform backend component objects into {status, detail} format
  const components: Record<string, ComponentHealth> = {};
  for (const [name, comp] of Object.entries(raw) as [string, any][]) {
    let status = 'ok';
    let detail = '';
    if (name === 'threads') {
      status = comp.warning ? 'warning' : 'ok';
      detail = `${comp.count} threads`;
    } else if (name === 'scanner') {
      status = comp.running ? 'ok' : 'warning';
      detail = comp.running ? 'Running' : 'Stopped';
    } else if (name === 'executor') {
      status = comp.initialized ? 'ok' : 'error';
      detail = comp.initialized ? `${comp.open_positions} positions` : 'Not initialized';
    } else if (name === 'exchange') {
      status = comp.initialized ? 'ok' : 'error';
      detail = comp.initialized ? 'Connected' : 'Disconnected';
    } else if (name === 'engine') {
      status = comp.running ? 'ok' : 'error';
      detail = comp.running ? (comp.trading_paused ? 'Paused' : 'Running') : 'Stopped';
    } else {
      status = comp.status || 'ok';
      detail = comp.detail || '';
    }
    components[name] = { status, detail };
  }
  return {
    components,
    thread_count: raw.threads?.count,
    uptime_s: raw.engine?.uptime_s,
  };
}

export async function getReadiness(): Promise<ReadinessReport> {
  const resp = await api.get('/validation/readiness');
  const d = resp.data;
  return {
    verdict: d.verdict || (d.ready ? 'READY' : 'NOT_READY'),
    score: d.score ?? (d.ready ? 100 : 0),
    checks: d.checks || [],
  };
}

export async function getDataIntegrity(): Promise<IntegrityReport> {
  const resp = await api.get('/validation/data-integrity');
  const d = resp.data;
  return {
    passed: d.passed ?? true,
    checks: d.checks || [],
  };
}
