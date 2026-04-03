import api from './client';

// ── Phase 4 Types ───────────────────────────────────────────

export interface EquityCurvePoint {
  time: number;
  capital: number;
  pnl: number;
}

export interface EquityCurveResponse {
  points: EquityCurvePoint[];
  initial_capital: number;
}

export interface PerformanceMetrics {
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  avg_r: number;
  max_drawdown_pct: number;
  sharpe_ratio: number;
  calmar_ratio: number;
  recovery_factor: number;
  best_trade: number;
  worst_trade: number;
  avg_win: number;
  avg_loss: number;
  win_streak: number;
  loss_streak: number;
}

export interface DistributionBucket {
  range_min: number;
  range_max: number;
  count: number;
}

export interface TradeDistribution {
  buckets: DistributionBucket[];
  mean: number;
  median: number;
  std: number;
}

export interface ModelBreakdown {
  name: string;
  trades: number;
  win_rate: number;
  pf: number;
  avg_r: number;
  max_dd?: number;
  expectancy?: number;
  best_trade?: number;
  worst_trade?: number;
  active?: boolean;
}

export interface ModelBreakdownResponse {
  models: ModelBreakdown[];
}

// ── Phase 5 Types ───────────────────────────────────────────

export interface DrawdownPoint {
  time: number;
  drawdown_pct: number;
  peak_capital: number;
}

export interface DrawdownCurveResponse {
  points: DrawdownPoint[];
}

export interface RollingMetricsPoint {
  time: number;
  rolling_wr: number;
  rolling_pf: number;
  rolling_avg_r: number;
}

export interface RollingMetricsResponse {
  points: RollingMetricsPoint[];
  window: number;
}

export interface RBucket {
  r_min: number;
  r_max: number;
  count: number;
}

export interface RDistributionResponse {
  buckets: RBucket[];
  expectancy: number;
  median_r: number;
  max_win_r: number;
  max_loss_r: number;
}

export interface DurationBucket {
  duration_min_s: number;
  duration_max_s: number;
  count: number;
  avg_r: number;
  win_rate: number;
}

export interface DurationAnalysisResponse {
  buckets: DurationBucket[];
}

export interface RegimePerformance {
  name: string;
  trades: number;
  win_rate: number;
  pf: number;
  avg_r: number;
  avg_duration_s: number;
  pct_of_total: number;
}

export interface RegimePerformanceResponse {
  regimes: RegimePerformance[];
}

export interface RegimeTransition {
  from: string;
  to: string;
  count: number;
  avg_pnl_during_transition: number;
}

export interface RegimeTransitionsResponse {
  transitions: RegimeTransition[];
}

// ── Phase 4 API Functions ───────────────────────────────────

export async function getEquityCurve(): Promise<EquityCurveResponse> {
  const resp = await api.get<EquityCurveResponse>('/analytics/equity-curve');
  return resp.data;
}

export async function getPerformanceMetrics(): Promise<PerformanceMetrics> {
  const resp = await api.get<PerformanceMetrics>('/analytics/metrics');
  return resp.data;
}

export async function getTradeDistribution(): Promise<TradeDistribution> {
  const resp = await api.get<TradeDistribution>('/analytics/trade-distribution');
  return resp.data;
}

export async function getModelBreakdown(params?: {
  sort?: string;
  order?: string;
  regime?: string;
  asset?: string;
}): Promise<ModelBreakdownResponse> {
  const resp = await api.get<ModelBreakdownResponse>('/analytics/by-model', { params });
  return resp.data;
}

// ── Phase 5 API Functions ───────────────────────────────────

export async function getDrawdownCurve(): Promise<DrawdownCurveResponse> {
  const resp = await api.get<DrawdownCurveResponse>('/analytics/drawdown-curve');
  return resp.data;
}

export async function getRollingMetrics(window = 20): Promise<RollingMetricsResponse> {
  const resp = await api.get<RollingMetricsResponse>('/analytics/rolling-metrics', {
    params: { window },
  });
  return resp.data;
}

export async function getRDistribution(): Promise<RDistributionResponse> {
  const resp = await api.get<RDistributionResponse>('/analytics/r-distribution');
  return resp.data;
}

export async function getDurationAnalysis(): Promise<DurationAnalysisResponse> {
  const resp = await api.get<DurationAnalysisResponse>('/analytics/duration-analysis');
  return resp.data;
}

export async function getPerformanceByRegime(): Promise<RegimePerformanceResponse> {
  const resp = await api.get<RegimePerformanceResponse>('/analytics/by-regime');
  return resp.data;
}

export async function getRegimeTransitions(): Promise<RegimeTransitionsResponse> {
  const resp = await api.get<RegimeTransitionsResponse>('/analytics/regime-transitions');
  return resp.data;
}
