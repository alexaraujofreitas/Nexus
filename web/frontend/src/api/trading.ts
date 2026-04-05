import api from './client';

export interface PaperPosition {
  symbol: string;
  side: string;
  entry_price: number;
  current_price: number;
  size_usdt: number;
  entry_size_usdt: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  stop_loss: number;
  take_profit: number;
  regime: string;
  models_fired: string[];
  opened_at: string;
  auto_partial_applied: boolean;
  breakeven_applied: boolean;
}

export interface PositionsResponse {
  positions: PaperPosition[];
  count: number;
}

export interface ClosedTrade {
  symbol: string;
  side: string;
  entry_price: number;
  exit_price: number;
  pnl_usdt: number;
  pnl_pct: number;
  duration_s: number;
  exit_reason: string;
  opened_at: string;
  closed_at: string;
  models_fired: string[];
  size_usdt?: number;
  entry_size_usdt?: number;
}

export interface TradeHistoryResponse {
  trades: ClosedTrade[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
  summary?: {
    wins: number;
    losses: number;
    total_pnl_usdt: number;
    total_pnl_pct: number;
  };
}

export async function getPositions(): Promise<PositionsResponse> {
  const resp = await api.get('/trading/positions');
  const d = resp.data;
  return {
    positions: d.positions || [],
    count: d.count ?? (d.positions || []).length,
  };
}

export async function closePosition(symbol: string): Promise<{ status: string; message: string }> {
  const resp = await api.post('/trading/close', { symbol });
  return resp.data;
}

export async function closeAllPositions(): Promise<{ status: string; message: string }> {
  const resp = await api.post('/trading/close-all');
  return resp.data;
}

export async function getTradeHistory(
  page: number = 1,
  perPage: number = 50,
): Promise<TradeHistoryResponse> {
  const resp = await api.get('/trades/history', {
    params: { page, per_page: perPage },
  });
  const d = resp.data;
  const trades = d.trades || [];
  // Backend may return total=0 but have trades — compute from array
  const total = d.total || trades.length;
  const wins = trades.filter((t: ClosedTrade) => (t.pnl_usdt ?? 0) > 0).length;
  const losses = trades.filter((t: ClosedTrade) => (t.pnl_usdt ?? 0) < 0).length;
  const totalPnl = trades.reduce((s: number, t: ClosedTrade) => s + (t.pnl_usdt ?? 0), 0);
  const totalPnlPct = trades.reduce((s: number, t: ClosedTrade) => s + (t.pnl_pct ?? 0), 0);
  return {
    trades,
    total,
    page: d.page ?? page,
    per_page: d.per_page ?? perPage,
    pages: d.pages || Math.ceil(total / perPage) || 1,
    summary: d.summary?.wins !== undefined && d.summary.wins + d.summary.losses > 0
      ? d.summary
      : { wins, losses, total_pnl_usdt: totalPnl, total_pnl_pct: totalPnlPct },
  };
}
