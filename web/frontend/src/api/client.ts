/**
 * Phase 6D: Hardened Axios API Client
 *
 * Features:
 *   - 15s timeout on all requests
 *   - Automatic retry with exponential backoff (3 retries for GET, 1 for mutations)
 *   - JWT token refresh on 401
 *   - Normalized error response format
 */
import axios, { type AxiosError, type InternalAxiosRequestConfig } from 'axios';

// ── Configuration ──────────────────────────────────────────
const API_TIMEOUT_MS = 15_000;
const MAX_GET_RETRIES = 3;
const MAX_MUTATION_RETRIES = 1;
const RETRY_BASE_DELAY_MS = 1_000;

// ── Extended config for retry tracking ─────────────────────
interface RetryConfig extends InternalAxiosRequestConfig {
  _retry?: boolean;
  _retryCount?: number;
}

// ── Normalized error shape ─────────────────────────────────
export interface ApiError {
  message: string;
  status: number;
  requestId?: string;
  errors?: Array<{ field: string; message: string }>;
}

export function normalizeError(error: AxiosError): ApiError {
  const resp = error.response;
  if (resp) {
    const data = resp.data as Record<string, unknown> | undefined;
    return {
      message: (data?.detail as string) || error.message || 'Request failed',
      status: resp.status,
      requestId: (resp.headers?.['x-request-id'] as string) || undefined,
      errors: (data?.errors as ApiError['errors']) || undefined,
    };
  }
  if (error.code === 'ECONNABORTED') {
    return { message: 'Request timed out', status: 0 };
  }
  return { message: error.message || 'Network error', status: 0 };
}

// ── Client ─────────────────────────────────────────────────
const api = axios.create({
  baseURL: '/api/v1',
  headers: { 'Content-Type': 'application/json' },
  timeout: API_TIMEOUT_MS,
});

// JWT interceptor: attach access token to every request
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token');
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// Response interceptor: retry + 401 refresh + error normalization
api.interceptors.response.use(
  (response) => response,
  async (error: AxiosError) => {
    const config = error.config as RetryConfig | undefined;
    if (!config) return Promise.reject(error);

    // ── Retry logic (network errors + 5xx) ────────────────
    const isRetryable =
      !error.response || // network error
      (error.response.status >= 500 && error.response.status < 600);
    const isGet = config.method?.toUpperCase() === 'GET';
    const maxRetries = isGet ? MAX_GET_RETRIES : MAX_MUTATION_RETRIES;
    const retryCount = config._retryCount || 0;

    if (isRetryable && retryCount < maxRetries) {
      config._retryCount = retryCount + 1;
      const delay = RETRY_BASE_DELAY_MS * Math.pow(2, retryCount);
      await new Promise((r) => setTimeout(r, delay));
      return api(config);
    }

    // ── 401 token refresh ─────────────────────────────────
    if (error.response?.status === 401 && !config._retry) {
      config._retry = true;
      const refreshToken = localStorage.getItem('refresh_token');
      if (refreshToken) {
        try {
          const { data } = await axios.post('/api/v1/auth/refresh', {
            refresh_token: refreshToken,
          });
          localStorage.setItem('access_token', data.access_token);
          if (data.refresh_token) {
            localStorage.setItem('refresh_token', data.refresh_token);
          }
          config.headers.Authorization = `Bearer ${data.access_token}`;
          return api(config);
        } catch {
          // Refresh failed — clear tokens and redirect to login
          localStorage.removeItem('access_token');
          localStorage.removeItem('refresh_token');
          window.location.href = '/login';
        }
      }
    }

    return Promise.reject(error);
  },
);

export default api;
