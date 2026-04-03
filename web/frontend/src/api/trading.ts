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
  closed_at: string;
  models_fired: string[];
}

export interface TradeHistoryResponse {
  trades: ClosedTrade[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export async function getPositions(): Promise<PositionsResponse> {
  const resp = await api.get<PositionsResponse>('/trading/positions');
  return resp.data;
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
  perPage: number = 20,
): Promise<TradeHistoryResponse> {
  const resp = await api.get<TradeHistoryResponse>('/trades/history', {
    params: { page, per_page: perPage },
  });
  return resp.data;
}
