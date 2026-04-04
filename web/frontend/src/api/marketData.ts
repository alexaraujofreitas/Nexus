/**
 * Phase 3A: Market Data API Client
 *
 * Fetches live market snapshots from the MarketDataService Redis cache
 * (with PostgreSQL fallback). Used by Asset Management to display
 * price, change, and volume columns.
 */
import api from './client';

export interface MarketSnapshot {
  asset_id: number;
  symbol: string;
  is_tradable: boolean;
  allocation_weight: number;
  snapshot: {
    price?: number;
    bid?: number;
    ask?: number;
    spread_pct?: number;
    change_1h?: number;
    change_4h?: number;
    change_24h?: number;
    volume_1h?: number;
    volume_24h?: number;
    high_24h?: number;
    low_24h?: number;
    vwap_24h?: number;
    [key: string]: unknown;
  } | null;
  snapshot_updated_at: string | null;
  data_source: 'redis' | 'db' | null;
}

export async function getSnapshots(): Promise<MarketSnapshot[]> {
  const resp = await api.get<{ snapshots: MarketSnapshot[]; count: number }>('/market-data/snapshots');
  return resp.data.snapshots;
}

export async function getSnapshot(assetId: number): Promise<MarketSnapshot> {
  const resp = await api.get<MarketSnapshot>(`/market-data/snapshots/${assetId}`);
  return resp.data;
}
