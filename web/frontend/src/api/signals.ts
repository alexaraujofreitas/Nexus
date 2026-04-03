import api from './client';

export interface AgentStatus {
  running: boolean;
  stale: boolean;
  signal: number;
  confidence: number;
  updated_at: string;
  errors: number;
}

export interface AgentStatusMap {
  [agentName: string]: AgentStatus;
}

export interface ConfluenceSignal {
  symbol: string;
  direction: string;
  score: number;
  models: string[];
  regime: string;
  entry_price: number | null;
  stop_loss: number | null;
  take_profit: number | null;
  approved: boolean;
  rejection_reason: string | null;
}

export interface ConfluenceResponse {
  signals: ConfluenceSignal[];
}

export async function getAgentStatuses(): Promise<AgentStatusMap> {
  const resp = await api.get<AgentStatusMap>('/signals/agents');
  return resp.data;
}

export async function getConfluenceSignals(): Promise<ConfluenceResponse> {
  const resp = await api.get<ConfluenceResponse>('/signals/confluence');
  return resp.data;
}
