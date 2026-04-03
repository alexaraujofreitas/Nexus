import api from './client';

export interface OHLCVBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface OHLCVResponse {
  bars: OHLCVBar[];
  symbol: string;
  timeframe: string;
}

export async function getOHLCV(
  symbol: string = 'BTC/USDT',
  timeframe: string = '30m',
  limit: number = 300,
): Promise<OHLCVResponse> {
  const resp = await api.get<OHLCVResponse>('/charts/ohlcv', {
    params: { symbol, timeframe, limit },
  });
  return resp.data;
}
