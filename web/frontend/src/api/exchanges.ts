/**
 * Phase 8B + Phase 3A: Exchange Management & Asset Management API Client
 *
 * CRUD operations for exchange connections, credential management,
 * connection testing, asset synchronization, and asset tradability control.
 */
import api from './client';

// ── Types ─────────────────────────────────────────────────

export interface SupportedExchange {
  exchange_id: string;
  name: string;
  has_sandbox: boolean;
  has_demo: boolean;
  needs_passphrase: boolean;
}

export interface ExchangeConfig {
  id: number;
  name: string;
  exchange_id: string;
  api_key_masked: string;
  api_secret_masked: string;
  passphrase_masked: string;
  has_api_key: boolean;
  has_api_secret: boolean;
  has_passphrase: boolean;
  sandbox_mode: boolean;
  demo_mode: boolean;
  mode: 'live' | 'sandbox' | 'demo';
  is_active: boolean;
  testnet_url: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ExchangeAsset {
  id: number;
  symbol: string;
  base_currency: string;
  quote_currency: string;
  price_precision: number;
  amount_precision: number;
  min_amount: number | null;
  min_cost: number | null;
  is_active: boolean;
  is_tradable: boolean;
  allocation_weight: number;
  market_snapshot: Record<string, unknown> | null;
  snapshot_updated_at: string | null;
  last_updated: string | null;
}

export interface ConnectionTestResult {
  status: string;
  message?: string;
  markets?: number;
  balance_usdt?: number;
  mode_label?: string;
  error?: string;
}

export interface AssetUpdate {
  is_tradable?: boolean;
  allocation_weight?: number;
}

export interface BulkAssetUpdate {
  asset_ids: number[];
  is_tradable?: boolean;
  allocation_weight?: number;
}

// ── Exchange API Calls ───────────────────────────────────

export async function getSupportedExchanges(): Promise<SupportedExchange[]> {
  const resp = await api.get('/exchanges/supported');
  return resp.data.exchanges;
}

export async function getExchanges(): Promise<ExchangeConfig[]> {
  const resp = await api.get('/exchanges/');
  return resp.data.exchanges;
}

export async function getExchange(id: number): Promise<ExchangeConfig> {
  const resp = await api.get(`/exchanges/${id}`);
  return resp.data;
}

export async function createExchange(data: {
  name: string;
  exchange_id: string;
  api_key?: string;
  api_secret?: string;
  passphrase?: string;
  mode?: string;
}): Promise<ExchangeConfig> {
  const resp = await api.post('/exchanges/', data);
  return resp.data;
}

export async function updateExchange(id: number, data: {
  name?: string;
  api_key?: string;
  api_secret?: string;
  passphrase?: string;
  mode?: string;
}): Promise<ExchangeConfig> {
  const resp = await api.put(`/exchanges/${id}`, data);
  return resp.data;
}

export async function deleteExchange(id: number): Promise<{ status: string; name: string }> {
  const resp = await api.delete(`/exchanges/${id}`);
  return resp.data;
}

export async function activateExchange(id: number): Promise<{ status: string; name: string; mode: string }> {
  const resp = await api.post(`/exchanges/${id}/activate`);
  return resp.data;
}

export async function deactivateExchange(id: number): Promise<{ status: string; name: string }> {
  const resp = await api.post(`/exchanges/${id}/deactivate`);
  return resp.data;
}

export async function testConnection(data: {
  exchange_id: string;
  api_key?: string;
  api_secret?: string;
  passphrase?: string;
  mode?: string;
}): Promise<ConnectionTestResult> {
  const resp = await api.post('/exchanges/test-connection', data);
  return resp.data;
}

// ── Asset Management API Calls ───────────────────────────

export async function getExchangeAssets(
  exchangeId: number,
  params?: { quote?: string; search?: string; is_tradable?: boolean },
): Promise<{ assets: ExchangeAsset[]; count: number; total: number }> {
  const resp = await api.get(`/exchanges/${exchangeId}/assets`, { params });
  return resp.data;
}

export async function getTradableAssets(
  exchangeId: number,
): Promise<{ symbols: string[]; assets: Array<{ id: number; symbol: string; allocation_weight: number }>; count: number }> {
  const resp = await api.get(`/exchanges/${exchangeId}/assets/tradable`);
  return resp.data;
}

export async function syncExchangeAssets(exchangeId: number): Promise<{ status: string; new_count?: number }> {
  const resp = await api.post(`/exchanges/${exchangeId}/sync-assets`);
  return resp.data;
}

export async function updateAsset(
  exchangeId: number,
  assetId: number,
  data: AssetUpdate,
): Promise<ExchangeAsset> {
  const resp = await api.patch(`/exchanges/${exchangeId}/assets/${assetId}`, data);
  return resp.data;
}

export async function bulkUpdateAssets(
  exchangeId: number,
  data: BulkAssetUpdate,
): Promise<{ updated: number; asset_ids: number[] }> {
  const resp = await api.patch(`/exchanges/${exchangeId}/assets/bulk`, data);
  return resp.data;
}
