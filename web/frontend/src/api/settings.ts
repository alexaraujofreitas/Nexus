import api from './client';

export async function getSettings(section?: string): Promise<Record<string, unknown>> {
  const params = section ? { section } : {};
  const resp = await api.get('/settings/', { params });
  return resp.data;
}

export async function updateSettings(updates: Record<string, unknown>): Promise<{ status: string }> {
  const resp = await api.patch('/settings/', { updates });
  return resp.data;
}
