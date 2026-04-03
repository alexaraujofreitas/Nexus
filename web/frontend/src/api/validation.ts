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
  const resp = await api.get<HealthReport>('/validation/health');
  return resp.data;
}

export async function getReadiness(): Promise<ReadinessReport> {
  const resp = await api.get<ReadinessReport>('/validation/readiness');
  return resp.data;
}

export async function getDataIntegrity(): Promise<IntegrityReport> {
  const resp = await api.get<IntegrityReport>('/validation/data-integrity');
  return resp.data;
}
