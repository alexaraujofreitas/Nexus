import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Settings2, Save, Check, Eye, EyeOff } from 'lucide-react';
import { getSettings, updateSettings } from '../api/settings';
import { useWSStore } from '../stores/wsStore';
import { cn } from '../lib/utils';

type Tab = 'risk' | 'strategy' | 'execution' | 'api_keys';

function get(obj: Record<string, any>, path: string, def: any = ''): any {
  return path.split('.').reduce((o, k) => (o && o[k] !== undefined ? o[k] : def), obj);
}

export default function Settings() {
  const [activeTab, setActiveTab] = useState<Tab>('risk');
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [draft, setDraft] = useState<Record<string, any>>({});
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => { if (status === 'connected') subscribe('engine'); }, [status, subscribe]);
  useEffect(() => {
    if (lastMessage['engine']?.type === 'config.changed') {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
    }
  }, [lastMessage, queryClient]);

  const { data: config } = useQuery({
    queryKey: ['settings'],
    queryFn: () => getSettings(),
    refetchInterval: 60000,
  });

  useEffect(() => { if (config) setDraft(config); }, [config]);

  const setVal = (path: string, value: any) => {
    setDraft((prev) => {
      const next = { ...prev };
      const keys = path.split('.');
      let cur: any = next;
      for (let i = 0; i < keys.length - 1; i++) {
        cur[keys[i]] = { ...(cur[keys[i]] || {}) };
        cur = cur[keys[i]];
      }
      cur[keys[keys.length - 1]] = value;
      return next;
    });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      await updateSettings(draft);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      queryClient.invalidateQueries({ queryKey: ['settings'] });
    } finally {
      setSaving(false);
    }
  };

  const tabs: { key: Tab; label: string }[] = [
    { key: 'risk', label: 'Risk' },
    { key: 'strategy', label: 'Strategy' },
    { key: 'execution', label: 'Execution' },
    { key: 'api_keys', label: 'API Keys' },
  ];

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-3">
        <Settings2 className="w-5 h-5 text-gray-400" />
        <h1 className="text-xl font-semibold text-gray-900">Settings</h1>
      </div>

      <div className="flex flex-col md:flex-row gap-4">
        {/* Tab sidebar */}
        <div className="flex md:flex-col gap-1 md:w-40 shrink-0">
          {tabs.map((t) => (
            <button
              key={t.key}
              onClick={() => setActiveTab(t.key)}
              className={cn(
                'px-3 py-2 rounded-lg text-sm font-medium text-left min-h-[44px] transition-colors',
                activeTab === t.key ? 'bg-blue-50 text-blue-700' : 'text-gray-600 hover:bg-gray-50',
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 bg-white rounded-lg border border-gray-200 p-4 md:p-6 space-y-6">
          {activeTab === 'risk' && <RiskTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'strategy' && <StrategyTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'execution' && <ExecutionTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'api_keys' && <APIKeysTab draft={draft} setVal={setVal} get={get} />}

          <div className="pt-4 border-t border-gray-100 flex justify-end">
            <button
              onClick={handleSave}
              disabled={saving}
              className={cn(
                'flex items-center gap-2 px-6 py-2 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
                saved ? 'bg-green-600 text-white' : 'bg-blue-600 text-white hover:bg-blue-700',
              )}
            >
              {saved ? <><Check className="w-4 h-4" /> Saved</> : <><Save className="w-4 h-4" /> Save Changes</>}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function NumberInput({ label, value, onChange, step = 0.01, min, max }: {
  label: string; value: number; onChange: (v: number) => void; step?: number; min?: number; max?: number;
}) {
  return (
    <div>
      <label className="block text-sm text-gray-600 mb-1">{label}</label>
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value) || 0)}
        step={step}
        min={min}
        max={max}
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
    </div>
  );
}

function Toggle({ label, value, onChange }: { label: string; value: boolean; onChange: (v: boolean) => void }) {
  return (
    <label className="flex items-center justify-between cursor-pointer py-2">
      <span className="text-sm text-gray-700">{label}</span>
      <button
        type="button"
        onClick={() => onChange(!value)}
        className={cn('relative w-11 h-6 rounded-full transition-colors', value ? 'bg-blue-600' : 'bg-gray-300')}
      >
        <span className={cn('absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full transition-transform', value && 'translate-x-5')} />
      </button>
    </label>
  );
}

function RiskTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Risk Parameters</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <NumberInput label="Risk % per trade" value={g(draft, 'risk_engine.risk_pct_per_trade', 0.5)} onChange={(v) => setVal('risk_engine.risk_pct_per_trade', v)} step={0.1} min={0.1} max={5} />
        <NumberInput label="Max capital % per trade" value={g(draft, 'risk_engine.max_capital_pct', 0.04)} onChange={(v) => setVal('risk_engine.max_capital_pct', v)} step={0.01} min={0.01} max={0.25} />
        <NumberInput label="Max portfolio heat %" value={g(draft, 'risk.max_portfolio_heat', 6)} onChange={(v) => setVal('risk.max_portfolio_heat', v)} step={1} min={1} max={100} />
        <NumberInput label="Max drawdown %" value={g(draft, 'risk.max_drawdown', 15)} onChange={(v) => setVal('risk.max_drawdown', v)} step={1} min={1} max={50} />
        <NumberInput label="Max open positions" value={g(draft, 'risk.max_open_positions', 5)} onChange={(v) => setVal('risk.max_open_positions', v)} step={1} min={1} max={20} />
      </div>
    </div>
  );
}

function StrategyTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const disabledModels: string[] = g(draft, 'disabled_models', []);
  const allModels = ['trend', 'momentum_breakout', 'pullback_long', 'swing_low_continuation', 'funding_rate', 'sentiment', 'mean_reversion', 'liquidity_sweep', 'donchian_breakout'];

  const toggleModel = (model: string) => {
    const current: string[] = [...disabledModels];
    const idx = current.indexOf(model);
    if (idx >= 0) current.splice(idx, 1);
    else current.push(model);
    setVal('disabled_models', current);
  };

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Strategy Controls</h2>
      <NumberInput label="Min confluence score" value={g(draft, 'idss.min_confluence_score', 0.45)} onChange={(v) => setVal('idss.min_confluence_score', v)} step={0.05} min={0.2} max={0.8} />
      <Toggle label="Multi-TF confirmation required" value={g(draft, 'multi_tf.confirmation_required', true)} onChange={(v) => setVal('multi_tf.confirmation_required', v)} />
      <div>
        <p className="text-sm text-gray-600 mb-2">Model Toggles</p>
        <div className="space-y-1">
          {allModels.map((m) => {
            const enabled = !disabledModels.includes(m);
            return (
              <Toggle key={m} label={m.replace(/_/g, ' ')} value={enabled} onChange={() => toggleModel(m)} />
            );
          })}
        </div>
      </div>
    </div>
  );
}

function ExecutionTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const tf = g(draft, 'data.default_timeframe', '30m');
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Execution Config</h2>
      <Toggle label="Auto-execute scans" value={g(draft, 'scanner.auto_execute', true)} onChange={(v) => setVal('scanner.auto_execute', v)} />
      <div>
        <label className="block text-sm text-gray-600 mb-1">Default Timeframe</label>
        <div className="flex gap-2">
          {['15m', '30m', '1h', '4h'].map((t) => (
            <button key={t} onClick={() => setVal('data.default_timeframe', t)} className={cn(
              'px-4 py-2 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
              tf === t ? 'bg-gray-900 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
            )}>{t}</button>
          ))}
        </div>
      </div>
      <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm text-amber-800">
        Paper trading mode (Phase 1) — live trading is disabled
      </div>
    </div>
  );
}

function APIKeysTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const keys = [
    { id: 'cryptopanic_api_key', label: 'CryptoPanic API Key', path: 'api_keys.cryptopanic' },
    { id: 'coinglass_api_key', label: 'Coinglass API Key', path: 'api_keys.coinglass' },
    { id: 'reddit_client_id', label: 'Reddit Client ID', path: 'api_keys.reddit_client_id' },
    { id: 'reddit_client_secret', label: 'Reddit Client Secret', path: 'api_keys.reddit_client_secret' },
  ];

  const mask = (val: string) => val ? `${'*'.repeat(Math.max(val.length - 4, 4))}${val.slice(-4)}` : '(not set)';

  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">API Keys</h2>
      {keys.map((k) => {
        const val = String(g(draft, k.path, '') || '');
        const shown = reveal[k.id];
        return (
          <div key={k.id}>
            <label className="block text-sm text-gray-600 mb-1">{k.label}</label>
            <div className="flex gap-2">
              <input
                type={shown ? 'text' : 'password'}
                value={val}
                onChange={(e) => setVal(k.path, e.target.value)}
                placeholder={val ? mask(val) : 'Not configured'}
                className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button onClick={() => setReveal((p) => ({ ...p, [k.id]: !p[k.id] }))} className="px-3 py-2 rounded-lg border border-gray-300 text-gray-500 hover:bg-gray-50 min-h-[44px]">
                {shown ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
            <p className="text-xs text-gray-400 mt-1">{val ? '✓ Configured' : '✗ Not set'}</p>
          </div>
        );
      })}
    </div>
  );
}
