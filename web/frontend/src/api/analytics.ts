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

// ── Regime Monitor Types ────────────────────────────────────

export interface CurrentRegime {
  regime: string;
  confidence: number;
  classifier: string;
  hmm_fitted: boolean;
  probabilities: Record<string, number>;
  description: string;
  strategies: string[];
  risk_adjustment: string;
  source: string;
}

export interface RegimeHistoryEntry {
  timestamp: string;
  regime: string;
  confidence: number;
  classifier: string;
}

export interface RegimeHistoryResponse {
  history: RegimeHistoryEntry[];
  source: string;
}

// ── Phase 4 API Functions ───────────────────────────────────

export async function getEquityCurve(): Promise<EquityCurveResponse> {
  const resp = await api.get('/analytics/equity-curve');
  const d = resp.data;
  const raw = d.equity_curve || d.points || [];
  // Ensure time values are valid and sorted ascending; use index as fallback
  const points = raw.map((p: any, i: number) => ({
    time: p.time && p.time > 0 ? p.time : i + 1,
    capital: p.capital ?? p.value ?? 0,
    pnl: p.pnl ?? 0,
  }));
  return { points, initial_capital: d.initial_capital ?? (points[0]?.capital ?? 0) };
}

export async function getPerformanceMetrics(): Promise<PerformanceMetrics> {
  const resp = await api.get('/analytics/metrics');
  const d = resp.data;
  const m = d.metrics || d;
  return {
    total_trades: m.total_trades ?? 0,
    win_rate: m.win_rate ?? 0,
    profit_factor: m.profit_factor ?? 0,
    avg_r: m.avg_r ?? 0,
    max_drawdown_pct: m.max_drawdown_pct ?? 0,
    sharpe_ratio: m.sharpe_ratio ?? 0,
    calmar_ratio: m.calmar_ratio ?? 0,
    recovery_factor: m.recovery_factor ?? 0,
    best_trade: m.best_trade ?? 0,
    worst_trade: m.worst_trade ?? 0,
    avg_win: m.avg_win ?? 0,
    avg_loss: m.avg_loss ?? 0,
    win_streak: m.win_streak ?? 0,
    loss_streak: m.loss_streak ?? 0,
  };
}

export async function getTradeDistribution(): Promise<TradeDistribution> {
  const resp = await api.get('/analytics/trade-distribution');
  const d = resp.data;
  return {
    buckets: d.buckets || [],
    mean: d.mean ?? 0,
    median: d.median ?? 0,
    std: d.std ?? 0,
  };
}

export async function getModelBreakdown(params?: {
  sort?: string;
  order?: string;
  regime?: string;
  asset?: string;
}): Promise<ModelBreakdownResponse> {
  const resp = await api.get('/analytics/by-model', { params });
  const d = resp.data;
  return { models: d.models || [] };
}

// ── Phase 5 API Functions ───────────────────────────────────

export async function getDrawdownCurve(): Promise<DrawdownCurveResponse> {
  const resp = await api.get('/analytics/drawdown-curve');
  const d = resp.data;
  const raw = d.points || [];
  // Fix time=0 by using index as fallback
  const points = raw.map((p: any, i: number) => ({
    time: p.time && p.time > 0 ? p.time : i + 1,
    drawdown_pct: p.drawdown_pct ?? 0,
    peak_capital: p.peak_capital ?? 0,
  }));
  return { points };
}

export async function getRollingMetrics(window = 20): Promise<RollingMetricsResponse> {
  const resp = await api.get('/analytics/rolling-metrics', { params: { window } });
  const d = resp.data;
  const raw = d.points || [];
  const points = raw.map((p: any, i: number) => ({
    time: p.time && p.time > 0 ? p.time : i + 1,
    rolling_wr: p.rolling_wr ?? 0,
    rolling_pf: Math.min(p.rolling_pf ?? 0, 100), // cap extreme PF values
    rolling_avg_r: p.rolling_avg_r ?? 0,
  }));
  return { points, window: d.window ?? window };
}

export async function getRDistribution(): Promise<RDistributionResponse> {
  const resp = await api.get('/analytics/r-distribution');
  const d = resp.data;
  return {
    buckets: d.buckets || [],
    expectancy: d.expectancy ?? 0,
    median_r: d.median_r ?? 0,
    max_win_r: d.max_win_r ?? 0,
    max_loss_r: d.max_loss_r ?? 0,
  };
}

export async function getDurationAnalysis(): Promise<DurationAnalysisResponse> {
  const resp = await api.get('/analytics/duration-analysis');
  const d = resp.data;
  return { buckets: d.buckets || [] };
}

export async function getPerformanceByRegime(): Promise<RegimePerformanceResponse> {
  const resp = await api.get('/analytics/by-regime');
  const d = resp.data;
  return { regimes: d.regimes || [] };
}

export async function getRegimeTransitions(): Promise<RegimeTransitionsResponse> {
  const resp = await api.get('/analytics/regime-transitions');
  const d = resp.data;
  return { transitions: d.transitions || [] };
}

// ── Regime Monitor API Functions ────────────────────────────

export async function getCurrentRegime(): Promise<CurrentRegime> {
  const resp = await api.get('/analytics/current-regime');
  const d = resp.data;
  const r = d.regime_data || d;
  return {
    regime: r.regime ?? 'uncertain',
    confidence: r.confidence ?? 0,
    classifier: r.classifier ?? '',
    hmm_fitted: r.hmm_fitted ?? false,
    probabilities: r.probabilities || {},
    description: r.description ?? '',
    strategies: r.strategies || [],
    risk_adjustment: r.risk_adjustment ?? '',
    source: r.source ?? '',
  };
}

export async function getRegimeHistory(): Promise<RegimeHistoryResponse> {
  const resp = await api.get('/analytics/regime-history');
  const d = resp.data;
  return {
    history: d.history || [],
    source: d.source ?? '',
  };
}
