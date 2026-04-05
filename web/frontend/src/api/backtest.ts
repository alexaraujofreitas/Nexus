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
  const resp = await api.post('/backtest/start', params);
  const d = resp.data;
  return { job_id: d.job_id, status: d.status || 'ok' };
}

export async function getBacktestStatus(jobId: string): Promise<BacktestStatus> {
  const resp = await api.get(`/backtest/status/${jobId}`);
  const d = resp.data;
  return {
    job_id: jobId,
    status: d.state || d.status || 'unknown',
    progress_pct: d.progress_pct ?? d.progress ?? 0,
    elapsed_s: d.elapsed_s ?? 0,
  };
}

export async function getBacktestResults(jobId: string): Promise<BacktestResults> {
  const resp = await api.get(`/backtest/results/${jobId}`);
  const d = resp.data;
  const r = d.results || {};
  return {
    job_id: jobId,
    status: d.state || d.status || 'unknown',
    metrics: {
      pf: r.profit_factor ?? 0,
      wr: r.win_rate ?? 0,
      max_dd: r.max_drawdown ?? 0,
      cagr: r.cagr ?? 0,
      n_trades: r.total_trades ?? 0,
      sharpe: r.sharpe ?? 0,
    },
    trades: r.trades || [],
  };
}
