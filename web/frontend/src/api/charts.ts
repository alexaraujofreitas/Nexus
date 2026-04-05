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

/**
 * Fetch OHLCV bars for a symbol/timeframe.
 * For higher timeframes (1D, 1W), requests more bars to ensure
 * enough data for 200-period indicators.
 */
export async function getOHLCV(
  symbol: string = 'BTC/USDT',
  timeframe: string = '1h',
  limit: number = 300,
): Promise<OHLCVResponse> {
  const resp = await api.get('/charts/ohlcv', {
    params: { symbol, timeframe, limit },
  });
  const d = resp.data;
  return {
    bars: d.bars || [],
    symbol: d.symbol || symbol,
    timeframe: d.timeframe || timeframe,
  };
}
