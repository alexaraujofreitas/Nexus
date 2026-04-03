import api from './client';

export interface LoginRequest {
  email: string;
  password: string;
}

export interface SetupRequest {
  email: string;
  password: string;
  display_name?: string;
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

export interface UserInfo {
  sub: string;
  email: string;
}

export async function login(data: LoginRequest): Promise<TokenResponse> {
  const resp = await api.post<TokenResponse>('/auth/login', data);
  return resp.data;
}

export async function setup(data: SetupRequest): Promise<TokenResponse> {
  const resp = await api.post<TokenResponse>('/auth/setup', data);
  return resp.data;
}

export async function getMe(): Promise<UserInfo> {
  const resp = await api.get<UserInfo>('/auth/me');
  return resp.data;
}

export async function logout(): Promise<void> {
  const refreshToken = localStorage.getItem('refresh_token');
  if (refreshToken) {
    await api.post('/auth/logout', { refresh_token: refreshToken });
  }
}
