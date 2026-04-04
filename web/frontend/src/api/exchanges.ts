/**
 * Phase 8B: Exchange Management API Client
 *
 * CRUD operations for exchange connections, credential management,
 * connection testing, and asset synchronization.
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

// ── API Calls ─────────────────────────────────────────────

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

export async function getExchangeAssets(
  exchangeId: number,
  params?: { quote?: string; search?: string },
): Promise<{ assets: ExchangeAsset[]; count: number }> {
  const resp = await api.get(`/exchanges/${exchangeId}/assets`, { params });
  return resp.data;
}

export async function syncExchangeAssets(exchangeId: number): Promise<{ status: string; new_count?: number }> {
  const resp = await api.post(`/exchanges/${exchangeId}/sync-assets`);
  return resp.data;
}
