import api from './client';

// ── Legacy types (kept for backward compatibility) ───────────
export interface OrderCandidate {
  symbol: string;
  direction: string;
  score: number;
  regime: string;
  models_fired: string[];
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  rr_ratio: number;
  approved: boolean;
  rejection_reason: string | null;
  position_size_usdt: number;
  generated_at: string;
}

export interface ScannerResults {
  results: OrderCandidate[];
  count: number;
  scanner_running: boolean;
}

export interface WatchlistResponse {
  symbols: string[];
  weights: Record<string, number>;
}

// ── Phase 3B: Pipeline Status types ──────────────────────────
export interface PipelineDiagnostics {
  candle_count: number;
  candle_age_s: number;
  candle_ts_str: string;
  regime_confidence: number;
  regime_probs: Record<string, number>;
  all_model_names: string[];
  models_disabled: string[];
  models_fired: string[];
  models_no_signal: string[];
  signal_details: Record<string, { direction: string; strength: number }>;
  indicator_cols_missing: string[];
  pre_filter_reason: string;
  rejection_reason: string;
  // ── MIL Phase 4A diagnostic keys (pass-through) ──────────
  mil_active?: boolean;
  mil_influence_pct?: number;
  mil_total_delta?: number;
  mil_capped?: boolean;
  mil_dominant_source?: string;
  mil_breakdown?: MILBreakdown;
  mil_funding_signal?: number;
  mil_funding_percentile?: number;
  mil_funding_divergence?: boolean;
  mil_funding_weighted_rate?: number;
  mil_oi_delta?: number;
  mil_oi_delta_4h?: number;
  mil_liquidation_proximity?: number;
  mil_oi_volume_ratio?: number;
  mil_orchestrator_meta?: number;
  mil_veto_active?: boolean;
}

// ── Phase S1: MIL breakdown (with Phase 4B placeholders) ────
export interface MILBreakdown {
  orchestrator_delta: number;
  sentiment_delta: number;       // Phase 4B placeholder (0.0 until wired)
  news_delta: number;            // Phase 4B placeholder (0.0 until wired)
  other_orchestrator_delta: number; // orchestrator minus sentiment/news attribution
  oi_delta: number;
  liquidation_delta: number;
}

export type PipelineStatus =
  | 'Eligible'
  | 'Risk Blocked'
  | 'No Signal'
  | 'Regime Filtered'
  | 'Pre-Filter'
  | 'Waiting'
  | 'Error';

export interface PipelineRow {
  asset_id: number;
  symbol: string;
  allocation_weight: number;
  price: number | null;
  regime: string;
  regime_confidence: number;
  models_fired: string[];
  models_no_signal: string[];
  score: number;
  direction: string;
  status: PipelineStatus;
  reason: string;
  is_approved: boolean;
  entry_price: number | null;
  stop_loss: number;
  take_profit: number;
  rr_ratio: number;
  position_size_usdt: number;
  scanned_at: string;
  // ── Phase S1: promoted MIL fields (top-level) ──────────────
  technical_score: number;
  final_score: number;
  mil_active: boolean;
  mil_total_delta: number;
  mil_influence_pct: number;
  mil_capped: boolean;
  mil_dominant_source: string;
  mil_breakdown: MILBreakdown | Record<string, never>;
  // ── Phase S1: decision explainability ──────────────────────
  decision_explanation: string;
  block_reasons: string[];
  // ── diagnostics (unchanged) ────────────────────────────────
  diagnostics: PipelineDiagnostics | Record<string, never>;
}

export interface PipelineSummary {
  total: number;
  eligible: number;
  active_signals: number;
  blocked: number;
}

export interface PipelineStatusResponse {
  status: string;
  pipeline: PipelineRow[];
  summary: PipelineSummary;
  scanner_running: boolean;
  last_scan_at: string;
  source: string;
}

// ── API functions ────────────────────────────────────────────
export async function getScannerResults(): Promise<ScannerResults> {
  const resp = await api.get<ScannerResults>('/scanner/results');
  return resp.data;
}

export async function getPipelineStatus(): Promise<PipelineStatusResponse> {
  const resp = await api.get<PipelineStatusResponse>('/scanner/pipeline-status');
  return resp.data;
}

export async function getWatchlist(): Promise<WatchlistResponse> {
  const resp = await api.get<WatchlistResponse>('/scanner/watchlist');
  return resp.data;
}

export async function triggerScan(): Promise<{ status: string; message: string }> {
  const resp = await api.post('/scanner/trigger');
  return resp.data;
}
