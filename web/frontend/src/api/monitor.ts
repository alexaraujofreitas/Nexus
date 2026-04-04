/**
 * NexusTrader — Demo Monitor API (Workstream 8H)
 * Real-time demo trading monitor endpoints.
 */
import api from './client';

// ── Types ────────────────────────────────────────────────

export interface MonitorPosition {
  symbol: string;
  side: 'long' | 'short';
  entry_price: number;
  current_price: number;
  size_usdt: number;
  pnl_unrealized: number;
  pnl_pct: number;
  duration_s: number;
  stop_loss: number | null;
  take_profit: number | null;
  regime_at_entry: string;
  models_fired: string[];
  score: number;
  opened_at: string;
  _auto_partial_applied: boolean;
  _breakeven_applied: boolean;
}

export interface PortfolioState {
  equity: number;
  balance: number;
  used_margin: number;
  free_margin: number;
  portfolio_heat_pct: number;
  max_heat_limit: number;
  drawdown_pct: number;
  total_return_pct: number;
  open_positions: number;
  total_trades: number;
  win_rate: number;
  profit_factor: number;
  trading_paused: boolean;
}

export interface LivePnL {
  total_unrealized: number;
  total_realized: number;
  daily_pnl: number;
  fees_paid: number;
  net_pnl: number;
}

export interface RiskState {
  drawdown_pct: number;
  daily_loss_pct: number;
  circuit_breaker_triggered: boolean;
  trading_enabled: boolean;
  crash_defense_tier: string;
  is_defensive: boolean;
  is_safe_mode: boolean;
  reason: string;
}

export interface MonitorTrade {
  symbol: string;
  side: string;
  entry_price: number;
  exit_price: number;
  pnl_usdt: number;
  pnl_pct: number;
  r_multiple: number;
  duration_s: number;
  regime: string;
  exit_reason: string;
  models_fired: string[];
  fees_estimated: number;
  slippage: number;
  closed_at: string;
  score: number;
}

// ── Response wrappers ──────────────────────────────────

export interface PositionsResponse {
  status: string;
  positions: MonitorPosition[];
  count: number;
  timestamp: string;
  data_source: string;
}

export interface PortfolioResponse {
  status: string;
  portfolio: PortfolioState;
  timestamp: string;
  data_source: string;
}

export interface PnLResponse {
  status: string;
  pnl: LivePnL;
  timestamp: string;
  data_source: string;
}

export interface RiskResponse {
  status: string;
  risk: RiskState;
  timestamp: string;
  data_source: string;
}

export interface TradesResponse {
  status: string;
  trades: MonitorTrade[];
  count: number;
  timestamp: string;
  data_source: string;
}

// ── API functions ──────────────────────────────────────

export async function getMonitorPositions(): Promise<PositionsResponse> {
  const res = await api.get('/monitor/positions');
  return res.data;
}

export async function getMonitorPortfolio(): Promise<PortfolioResponse> {
  const res = await api.get('/monitor/portfolio');
  return res.data;
}

export async function getMonitorPnL(): Promise<PnLResponse> {
  const res = await api.get('/monitor/pnl');
  return res.data;
}

export async function getMonitorRisk(): Promise<RiskResponse> {
  const res = await api.get('/monitor/risk');
  return res.data;
}

export async function getMonitorTrades(): Promise<TradesResponse> {
  const res = await api.get('/monitor/trades');
  return res.data;
}
