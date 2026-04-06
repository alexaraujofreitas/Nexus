import api from './client';

export interface AgentStatus {
  running: boolean;
  stale: boolean;
  signal: number;
  confidence: number;
  has_data: boolean;
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

/**
 * Fetch agent statuses. If the backend returns empty agents (headless web mode
 * where AgentCoordinator has no agents), falls back to the pipeline-status
 * endpoint and synthesizes agent cards from model scan data.
 */
export async function getAgentStatuses(): Promise<AgentStatusMap> {
  const resp = await api.get('/signals/agents');
  const d = resp.data;

  // Extract agents from envelope
  let agents: AgentStatusMap = {};
  if (d.agents && typeof d.agents === 'object' && Object.keys(d.agents).length > 0) {
    agents = d.agents;
  }

  // If empty, synthesize from pipeline scan results
  if (Object.keys(agents).length === 0) {
    try {
      const pipeResp = await api.get('/scanner/pipeline-status');
      const pd = pipeResp.data;
      const pipeline = pd.pipeline || [];

      for (const asset of pipeline) {
        if (!asset || typeof asset !== 'object') continue;
        const rawTs = asset.scanned_at || '';
        // Ensure UTC: append Z if no timezone suffix
        const scannedAt = rawTs && !rawTs.endsWith('Z') && !rawTs.includes('+') ? rawTs + 'Z' : rawTs;
        const regimeConf = asset.regime_confidence || 0;

        for (const m of (asset.models_fired || [])) {
          if (!agents[m]) {
            agents[m] = {
              running: true,
              stale: false,
              signal: Math.round((asset.score || 0) * 10000) / 10000,
              confidence: Math.round(regimeConf * 10000) / 10000,
              updated_at: scannedAt,
              errors: 0,
            };
          }
        }
        for (const m of (asset.models_no_signal || [])) {
          if (!agents[m]) {
            agents[m] = {
              running: true,
              stale: false,
              signal: 0,
              confidence: Math.round(regimeConf * 10000) / 10000,
              updated_at: scannedAt,
              errors: 0,
            };
          }
        }
      }
    } catch {
      // Pipeline endpoint failed — return empty
    }
  }

  return agents;
}

/**
 * Fetch confluence signals. Falls back to pipeline data if the signals
 * endpoint returns empty.
 */
export async function getConfluenceSignals(): Promise<ConfluenceResponse> {
  const resp = await api.get('/signals/confluence');
  const d = resp.data;
  let signals = d.signals || [];

  // If empty, try pipeline
  if (signals.length === 0) {
    try {
      const pipeResp = await api.get('/scanner/pipeline-status');
      const pd = pipeResp.data;
      const pipeline = pd.pipeline || [];

      for (const r of pipeline) {
        if (!r || typeof r !== 'object') continue;
        // Include all scanned assets as signal cards (even with score=0)
        signals.push({
          symbol: r.symbol || '',
          direction: r.direction || '',
          score: r.score || 0,
          models: r.models_fired || [],
          regime: r.regime || '',
          entry_price: r.entry_price || null,
          stop_loss: r.stop_loss || null,
          take_profit: r.take_profit || null,
          approved: r.is_approved || false,
          rejection_reason: r.reason || r.status || '',
        });
      }
    } catch {
      // Pipeline endpoint failed
    }
  }

  return { signals };
}
