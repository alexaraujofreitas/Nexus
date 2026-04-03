import api from './client';

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

export async function getScannerResults(): Promise<ScannerResults> {
  const resp = await api.get<ScannerResults>('/scanner/results');
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
