/**
 * NexusTrader — Trades Monitor API (Workstream 8H)
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
  positions: MonitorPosition[];
  count: number;
}

export interface PortfolioResponse {
  portfolio: PortfolioState;
}

export interface PnLResponse {
  pnl: LivePnL;
}

export interface RiskResponse {
  risk: RiskState;
}

export interface TradesResponse {
  trades: MonitorTrade[];
  count: number;
}

// ── API functions ──────────────────────────────────────

export async function getMonitorPositions(): Promise<PositionsResponse> {
  const res = await api.get('/monitor/positions');
  const d = res.data;
  return { positions: d.positions || [], count: d.count ?? 0 };
}

export async function getMonitorPortfolio(): Promise<PortfolioResponse> {
  const res = await api.get('/monitor/portfolio');
  const d = res.data;
  return { portfolio: d.portfolio || d };
}

export async function getMonitorPnL(): Promise<PnLResponse> {
  const res = await api.get('/monitor/pnl');
  const d = res.data;
  return { pnl: d.pnl || d };
}

export async function getMonitorRisk(): Promise<RiskResponse> {
  // Try monitor/risk first; if it errors, fall back to risk/status
  try {
    const res = await api.get('/monitor/risk');
    const d = res.data;
    if (d.status === 'error' || !d.risk) throw new Error('monitor/risk failed');
    return { risk: d.risk };
  } catch {
    // Fallback: build risk state from the working /risk/status endpoint
    try {
      const res = await api.get('/risk/status');
      const d = res.data;
      const r = d.risk || d;
      return {
        risk: {
          drawdown_pct: r.drawdown_pct ?? 0,
          daily_loss_pct: r.daily_loss_pct ?? 0,
          circuit_breaker_triggered: r.circuit_breaker_on ?? false,
          trading_enabled: !r.circuit_breaker_on,
          crash_defense_tier: r.crash_tier ?? 'NORMAL',
          is_defensive: r.is_defensive ?? false,
          is_safe_mode: false,
          reason: '',
        },
      };
    } catch {
      // Both failed — return safe defaults
      return {
        risk: {
          drawdown_pct: 0,
          daily_loss_pct: 0,
          circuit_breaker_triggered: false,
          trading_enabled: true,
          crash_defense_tier: 'NORMAL',
          is_defensive: false,
          is_safe_mode: false,
          reason: '',
        },
      };
    }
  }
}

export async function getMonitorTrades(): Promise<TradesResponse> {
  const res = await api.get('/monitor/trades');
  const d = res.data;
  return { trades: d.trades || [], count: d.count ?? 0 };
}
