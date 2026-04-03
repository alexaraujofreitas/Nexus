import api from './client';

export interface BacktestParams {
  symbols: string[];
  start_date: string;
  end_date: string;
  timeframe: string;
  fee_pct: number;
}

export interface BacktestStartResponse {
  job_id: string;
  status: string;
}

export interface BacktestStatus {
  job_id: string;
  status: string;
  progress_pct: number;
  elapsed_s: number;
}

export interface BacktestMetrics {
  pf: number;
  wr: number;
  max_dd: number;
  cagr: number;
  n_trades: number;
  sharpe: number;
}

export interface BacktestResults {
  job_id: string;
  status: string;
  metrics: BacktestMetrics;
  trades: Array<Record<string, unknown>>;
}

export async function startBacktest(params: BacktestParams): Promise<BacktestStartResponse> {
  const resp = await api.post<BacktestStartResponse>('/backtest/start', params);
  return resp.data;
}

export async function getBacktestStatus(jobId: string): Promise<BacktestStatus> {
  const resp = await api.get<BacktestStatus>(`/backtest/status/${jobId}`);
  return resp.data;
}

export async function getBacktestResults(jobId: string): Promise<BacktestResults> {
  const resp = await api.get<BacktestResults>(`/backtest/results/${jobId}`);
  return resp.data;
}
