import { useEffect, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Settings2, Save, Check, Eye, EyeOff, Send, Loader2, CheckCircle, XCircle } from 'lucide-react';
import { getSettings, updateSettings } from '../api/settings';
import { testNotificationChannel, testAllNotificationChannels, type NotificationChannel } from '../api/notifications';
import { useWSStore } from '../stores/wsStore';
import { cn } from '../lib/utils';

type Tab = 'risk' | 'strategy' | 'execution' | 'ai_ml' | 'data_sentiment' | 'notifications' | 'backtesting' | 'agents' | 'portfolio' | 'api_keys';

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
    { key: 'ai_ml', label: 'AI & ML' },
    { key: 'data_sentiment', label: 'Data & Feeds' },
    { key: 'notifications', label: 'Notifications' },
    { key: 'backtesting', label: 'Backtesting' },
    { key: 'agents', label: 'Agents' },
    { key: 'portfolio', label: 'Portfolio' },
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
          {activeTab === 'ai_ml' && <AIMLTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'data_sentiment' && <DataSentimentTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'notifications' && <NotificationsTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'backtesting' && <BacktestingTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'agents' && <AgentsTab draft={draft} setVal={setVal} get={get} />}
          {activeTab === 'portfolio' && <PortfolioAllocationTab draft={draft} setVal={setVal} get={get} />}
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
    <div className="space-y-6">
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Position & Portfolio Risk</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput label="Risk % per trade" value={g(draft, 'risk_engine.risk_pct_per_trade', 0.5)} onChange={(v) => setVal('risk_engine.risk_pct_per_trade', v)} step={0.1} min={0.1} max={5} />
          <NumberInput label="Max capital % per trade" value={g(draft, 'risk_engine.max_capital_pct', 0.04)} onChange={(v) => setVal('risk_engine.max_capital_pct', v)} step={0.01} min={0.01} max={0.25} />
          <NumberInput label="Max position size %" value={g(draft, 'risk.max_position_pct', 2.0)} onChange={(v) => setVal('risk.max_position_pct', v)} step={0.5} min={0.5} max={10} />
          <NumberInput label="Max portfolio drawdown %" value={g(draft, 'risk.max_portfolio_drawdown_pct', 15.0)} onChange={(v) => setVal('risk.max_portfolio_drawdown_pct', v)} step={1} min={1} max={50} />
          <NumberInput label="Max strategy drawdown %" value={g(draft, 'risk.max_strategy_drawdown_pct', 10.0)} onChange={(v) => setVal('risk.max_strategy_drawdown_pct', v)} step={1} min={1} max={50} />
          <NumberInput label="Max portfolio heat %" value={g(draft, 'risk.max_portfolio_heat', 6)} onChange={(v) => setVal('risk.max_portfolio_heat', v)} step={1} min={1} max={100} />
          <NumberInput label="Min Sharpe (live)" value={g(draft, 'risk.min_sharpe_live', 0.5)} onChange={(v) => setVal('risk.min_sharpe_live', v)} step={0.1} min={0} max={3} />
          <NumberInput label="Max spread filter %" value={g(draft, 'risk.max_spread_pct', 0.3)} onChange={(v) => setVal('risk.max_spread_pct', v)} step={0.05} min={0.01} max={2} />
        </div>
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Default Stop / Take Profit</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput label="Default stop-loss %" value={g(draft, 'risk.default_stop_loss_pct', 2.0)} onChange={(v) => setVal('risk.default_stop_loss_pct', v)} step={0.5} min={0.5} max={10} />
          <NumberInput label="Default take-profit %" value={g(draft, 'risk.default_take_profit_pct', 4.0)} onChange={(v) => setVal('risk.default_take_profit_pct', v)} step={0.5} min={0.5} max={20} />
        </div>
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">IDSS Scanner — RiskGate & Confluence</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput label="Max concurrent positions" value={g(draft, 'risk.max_concurrent_positions', 3)} onChange={(v) => setVal('risk.max_concurrent_positions', v)} step={1} min={1} max={10} />
          <NumberInput label="Min risk:reward ratio" value={g(draft, 'risk.min_risk_reward', 1.3)} onChange={(v) => setVal('risk.min_risk_reward', v)} step={0.1} min={0.5} max={5} />
          <NumberInput label="Min confluence score" value={g(draft, 'idss.min_confluence_score', 0.55)} onChange={(v) => setVal('idss.min_confluence_score', v)} step={0.05} min={0.2} max={0.8} />
          <NumberInput label="Max open positions" value={g(draft, 'risk.max_open_positions', 5)} onChange={(v) => setVal('risk.max_open_positions', v)} step={1} min={1} max={20} />
        </div>
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

// ── Reusable components for vault key fields ──────────────

function SelectInput({ label, value, options, onChange }: {
  label: string; value: string; options: string[]; onChange: (v: string) => void;
}) {
  return (
    <div>
      <label className="block text-sm text-gray-600 mb-1">{label}</label>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
      >
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
      </select>
    </div>
  );
}

function TextInput({ label, value, onChange, placeholder }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  return (
    <div>
      <label className="block text-sm text-gray-600 mb-1">{label}</label>
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
      />
    </div>
  );
}

function VaultKeyInput({ label, value, onChange, placeholder }: {
  id?: string; label: string; value: string; onChange: (v: string) => void; placeholder?: string;
}) {
  const [show, setShow] = useState(false);
  return (
    <div>
      <label className="block text-sm text-gray-600 mb-1">{label}</label>
      <div className="flex gap-2">
        <input
          type={show ? 'text' : 'password'}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder || 'Not configured'}
          className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button onClick={() => setShow(!show)} className="px-3 py-2 rounded-lg border border-gray-300 text-gray-500 hover:bg-gray-50 min-h-[44px]">
          {show ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
        </button>
      </div>
      <p className="text-xs text-gray-400 mt-1">{value ? '\u2713 Configured \u2014 stored encrypted' : '\u2717 Not set'}</p>
    </div>
  );
}

// ── AI & ML Tab ───────────────────────────────────────────

function AIMLTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const providers = [
    'Auto (Anthropic \u2192 OpenAI \u2192 Gemini)',
    'Anthropic Claude',
    'OpenAI',
    'Google Gemini',
    'Local (Ollama)',
  ];
  const anthropicModels = ['claude-opus-4-6', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'claude-sonnet-4-20250514'];
  const openaiModels = ['gpt-4o', 'gpt-4-turbo', 'gpt-4', 'gpt-3.5-turbo'];
  const geminiModels = ['gemini-2.0-flash', 'gemini-2.5-pro-exp-03-25', 'gemini-1.5-pro', 'gemini-1.5-flash'];
  const ollamaModels = ['deepseek-r1:14b', 'deepseek-r1:7b', 'qwen2.5:14b', 'qwen2.5:7b', 'llama3.1:8b', 'mistral:7b', 'phi4:14b'];

  return (
    <div className="space-y-6">
      {/* AI Provider Configuration */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">AI & Language Models</h2>

        <SelectInput
          label="Active AI Provider"
          value={g(draft, 'ai.active_provider', 'Auto (Anthropic \u2192 OpenAI \u2192 Gemini)')}
          options={providers}
          onChange={(v) => setVal('ai.active_provider', v)}
        />

        <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
          {/* Anthropic */}
          <div className="space-y-3 p-3 bg-gray-50 rounded-lg">
            <p className="text-xs font-medium text-gray-500 uppercase">Anthropic</p>
            <VaultKeyInput
              id="anthropic_key"
              label="API Key"
              value={String(g(draft, 'ai.anthropic_api_key', '') || '')}
              onChange={(v) => setVal('ai.anthropic_api_key', v)}
              placeholder="sk-ant-... (stored encrypted)"
            />
            <SelectInput
              label="Model"
              value={g(draft, 'ai.anthropic_model', 'claude-opus-4-6')}
              options={anthropicModels}
              onChange={(v) => setVal('ai.anthropic_model', v)}
            />
          </div>

          {/* OpenAI */}
          <div className="space-y-3 p-3 bg-gray-50 rounded-lg">
            <p className="text-xs font-medium text-gray-500 uppercase">OpenAI</p>
            <VaultKeyInput
              id="openai_key"
              label="API Key"
              value={String(g(draft, 'ai.openai_api_key', '') || '')}
              onChange={(v) => setVal('ai.openai_api_key', v)}
              placeholder="sk-... (stored encrypted)"
            />
            <SelectInput
              label="Model"
              value={g(draft, 'ai.openai_model', 'gpt-4o')}
              options={openaiModels}
              onChange={(v) => setVal('ai.openai_model', v)}
            />
          </div>

          {/* Gemini */}
          <div className="space-y-3 p-3 bg-gray-50 rounded-lg">
            <p className="text-xs font-medium text-gray-500 uppercase">Google Gemini</p>
            <VaultKeyInput
              id="gemini_key"
              label="API Key"
              value={String(g(draft, 'ai.gemini_api_key', '') || '')}
              onChange={(v) => setVal('ai.gemini_api_key', v)}
              placeholder="AIza... (stored encrypted)"
            />
            <SelectInput
              label="Model"
              value={g(draft, 'ai.gemini_model', 'gemini-2.0-flash')}
              options={geminiModels}
              onChange={(v) => setVal('ai.gemini_model', v)}
            />
          </div>

          {/* Ollama */}
          <div className="space-y-3 p-3 bg-gray-50 rounded-lg">
            <p className="text-xs font-medium text-gray-500 uppercase">Local (Ollama)</p>
            <SelectInput
              label="Model"
              value={g(draft, 'ai.ollama_model', 'deepseek-r1:14b')}
              options={ollamaModels}
              onChange={(v) => setVal('ai.ollama_model', v)}
            />
            <TextInput
              label="Ollama URL"
              value={g(draft, 'ai.ollama_url', 'http://localhost:11434/v1')}
              onChange={(v) => setVal('ai.ollama_url', v)}
              placeholder="http://localhost:11434/v1"
            />
          </div>
        </div>
      </div>

      {/* ML Configuration */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">ML Configuration</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput
            label="ML Confidence Threshold"
            value={g(draft, 'ai.ml_confidence_threshold', 0.65)}
            onChange={(v) => setVal('ai.ml_confidence_threshold', v)}
            step={0.05} min={0.5} max={0.99}
          />
          <NumberInput
            label="Model Retrain Interval (hours)"
            value={g(draft, 'ai.retrain_interval_hours', 24)}
            onChange={(v) => setVal('ai.retrain_interval_hours', v)}
            step={1} min={1} max={168}
          />
        </div>
        <Toggle
          label="Enable AI Strategy Generation"
          value={g(draft, 'ai.strategy_generation_enabled', true)}
          onChange={(v) => setVal('ai.strategy_generation_enabled', v)}
        />
      </div>

      {/* RL Configuration */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Reinforcement Learning</h2>
        <Toggle
          label="RL Ensemble enabled"
          value={g(draft, 'rl.enabled', false)}
          onChange={(v) => setVal('rl.enabled', v)}
        />
        <Toggle
          label="Shadow-only mode (observe, don't trade)"
          value={g(draft, 'rl.shadow_only', true)}
          onChange={(v) => setVal('rl.shadow_only', v)}
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput
            label="Replay Buffer Size"
            value={g(draft, 'rl.replay_buffer_size', 50000)}
            onChange={(v) => setVal('rl.replay_buffer_size', v)}
            step={1000} min={1000} max={500000}
          />
          <NumberInput
            label="Train Every N Candles"
            value={g(draft, 'rl.train_every_n_candles', 10)}
            onChange={(v) => setVal('rl.train_every_n_candles', v)}
            step={1} min={1} max={100}
          />
          <NumberInput
            label="Reward Leverage"
            value={g(draft, 'rl.reward_leverage', 10.0)}
            onChange={(v) => setVal('rl.reward_leverage', v)}
            step={1} min={1} max={50}
          />
        </div>
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-sm text-blue-800">
          RL is currently in shadow-only mode. Ensemble weight is 0.0 until validated on live data.
        </div>
      </div>

      {/* Regime Configuration */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Regime Classifier</h2>
        <Toggle
          label="Use ensemble (HMM + rule-based)"
          value={g(draft, 'regime.use_ensemble', true)}
          onChange={(v) => setVal('regime.use_ensemble', v)}
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <NumberInput
            label="HMM Weight"
            value={g(draft, 'regime.hmm_weight', 0.35)}
            onChange={(v) => setVal('regime.hmm_weight', v)}
            step={0.05} min={0} max={1}
          />
          <NumberInput
            label="Rule-Based Weight"
            value={g(draft, 'regime.rule_weight', 0.65)}
            onChange={(v) => setVal('regime.rule_weight', v)}
            step={0.05} min={0} max={1}
          />
        </div>
      </div>
    </div>
  );
}

// ── Data & Sentiment Tab ──────────────────────────────────

function DataSentimentTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  return (
    <div className="space-y-6">
      {/* Data & Market Feeds */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Data & Market Feeds</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm text-gray-600 mb-1">Default Timeframe</label>
            <div className="flex gap-2">
              {['1m', '5m', '15m', '1h', '4h', '1d'].map((t) => (
                <button
                  key={t}
                  onClick={() => setVal('data.default_timeframe', t)}
                  className={cn(
                    'px-3 py-1.5 rounded-lg text-sm font-medium min-h-[36px] transition-colors',
                    g(draft, 'data.default_timeframe', '1h') === t
                      ? 'bg-gray-900 text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200',
                  )}
                >{t}</button>
              ))}
            </div>
          </div>
          <NumberInput
            label="Historical Data (days)"
            value={g(draft, 'data.historical_days', 365)}
            onChange={(v) => setVal('data.historical_days', v)}
            step={30} min={30} max={1825}
          />
        </div>
        <Toggle
          label="Enable Data Cache"
          value={g(draft, 'data.cache_enabled', true)}
          onChange={(v) => setVal('data.cache_enabled', v)}
        />
        <Toggle
          label="WebSocket Streaming"
          value={g(draft, 'data.websocket_enabled', false)}
          onChange={(v) => setVal('data.websocket_enabled', v)}
        />
        <NumberInput
          label="Feed Interval (seconds)"
          value={g(draft, 'data.feed_interval_seconds', 3)}
          onChange={(v) => setVal('data.feed_interval_seconds', v)}
          step={1} min={1} max={60}
        />
      </div>

      {/* Sentiment Data Sources */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Sentiment Data Sources</h2>

        <Toggle
          label="Crypto News API"
          value={g(draft, 'sentiment.news_enabled', true)}
          onChange={(v) => setVal('sentiment.news_enabled', v)}
        />
        <VaultKeyInput
          id="news_api_key"
          label="News API Key"
          value={String(g(draft, 'sentiment.news_api_key', '') || '')}
          onChange={(v) => setVal('sentiment.news_api_key', v)}
          placeholder="NewsAPI.org key (stored encrypted)"
        />
        <VaultKeyInput
          id="cryptopanic_key"
          label="CryptoPanic API Key"
          value={String(g(draft, 'agents.cryptopanic_api_key', '') || '')}
          onChange={(v) => setVal('agents.cryptopanic_api_key', v)}
          placeholder="CryptoPanic key (stored encrypted)"
        />

        <div className="pt-2 border-t border-gray-100" />

        <Toggle
          label="Reddit Sentiment"
          value={g(draft, 'sentiment.reddit_enabled', false)}
          onChange={(v) => setVal('sentiment.reddit_enabled', v)}
        />
        <VaultKeyInput
          id="reddit_cid"
          label="Reddit Client ID"
          value={String(g(draft, 'sentiment.reddit_client_id', '') || '')}
          onChange={(v) => setVal('sentiment.reddit_client_id', v)}
          placeholder="Reddit app client_id (stored encrypted)"
        />
        <VaultKeyInput
          id="reddit_secret"
          label="Reddit Client Secret"
          value={String(g(draft, 'sentiment.reddit_client_secret', '') || '')}
          onChange={(v) => setVal('sentiment.reddit_client_secret', v)}
          placeholder="Reddit app client_secret (stored encrypted)"
        />

        <div className="pt-2 border-t border-gray-100" />

        <Toggle
          label="Twitter Sentiment"
          value={g(draft, 'sentiment.twitter_enabled', false)}
          onChange={(v) => setVal('sentiment.twitter_enabled', v)}
        />
        <Toggle
          label="On-Chain Data"
          value={g(draft, 'sentiment.onchain_enabled', false)}
          onChange={(v) => setVal('sentiment.onchain_enabled', v)}
        />
        <NumberInput
          label="Sentiment Update Interval (minutes)"
          value={g(draft, 'sentiment.update_interval_minutes', 15)}
          onChange={(v) => setVal('sentiment.update_interval_minutes', v)}
          step={5} min={5} max={120}
        />
      </div>
    </div>
  );
}

// ── Notifications Tab ────────────────────────────────────────

function NotificationsTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const [testResults, setTestResults] = useState<Record<string, 'idle' | 'testing' | 'success' | 'failed'>>({});
  const [testAllLoading, setTestAllLoading] = useState(false);

  const handleTestChannel = async (channel: NotificationChannel) => {
    setTestResults((p) => ({ ...p, [channel]: 'testing' }));
    try {
      await testNotificationChannel(channel);
      setTestResults((p) => ({ ...p, [channel]: 'success' }));
    } catch {
      setTestResults((p) => ({ ...p, [channel]: 'failed' }));
    }
    setTimeout(() => setTestResults((p) => ({ ...p, [channel]: 'idle' })), 5000);
  };

  const handleTestAll = async () => {
    setTestAllLoading(true);
    try {
      const result = await testAllNotificationChannels();
      const updated: Record<string, 'idle' | 'testing' | 'success' | 'failed'> = {};
      for (const [ch, ok] of Object.entries(result)) {
        updated[ch] = ok ? 'success' : 'failed';
      }
      setTestResults((p) => ({ ...p, ...updated }));
      setTimeout(() => {
        setTestResults((p) => {
          const reset = { ...p };
          for (const ch of Object.keys(updated)) reset[ch] = 'idle';
          return reset;
        });
      }, 5000);
    } catch {
      // Silently handle
    } finally {
      setTestAllLoading(false);
    }
  };

  const TestButton = ({ channel, label }: { channel: NotificationChannel; label: string }) => {
    const st = testResults[channel] || 'idle';
    return (
      <button
        onClick={() => handleTestChannel(channel)}
        disabled={st === 'testing'}
        className={cn(
          'flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium min-h-[32px] transition-colors',
          st === 'success' ? 'bg-green-100 text-green-700' :
          st === 'failed' ? 'bg-red-100 text-red-700' :
          st === 'testing' ? 'bg-gray-100 text-gray-400' :
          'bg-blue-50 text-blue-700 hover:bg-blue-100',
        )}
      >
        {st === 'testing' && <Loader2 className="w-3 h-3 animate-spin" />}
        {st === 'success' && <CheckCircle className="w-3 h-3" />}
        {st === 'failed' && <XCircle className="w-3 h-3" />}
        {st === 'idle' && <Send className="w-3 h-3" />}
        {st === 'success' ? 'Sent' : st === 'failed' ? 'Failed' : st === 'testing' ? 'Sending...' : label}
      </button>
    );
  };

  const healthCheckIntervals = ['1', '2', '3', '4', '6', '12', '24'];

  return (
    <div className="space-y-6">
      {/* Global Settings */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">General</h2>
        <Toggle
          label="Desktop Notifications"
          value={g(draft, 'notifications.desktop_enabled', true)}
          onChange={(v) => setVal('notifications.desktop_enabled', v)}
        />
        <Toggle
          label="Sound Alerts"
          value={g(draft, 'notifications.sound_enabled', false)}
          onChange={(v) => setVal('notifications.sound_enabled', v)}
        />
        <NumberInput
          label="Dedup Window (seconds)"
          value={g(draft, 'notifications.dedup_window_seconds', 60)}
          onChange={(v) => setVal('notifications.dedup_window_seconds', v)}
          step={10} min={10} max={600}
        />
      </div>

      {/* WhatsApp Channel */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">WhatsApp (Twilio)</h2>
          <TestButton channel="whatsapp" label="Test" />
        </div>
        <Toggle
          label="Enable WhatsApp"
          value={g(draft, 'notifications.whatsapp.enabled', false)}
          onChange={(v) => setVal('notifications.whatsapp.enabled', v)}
        />
        <VaultKeyInput
          id="twilio_sid"
          label="Twilio Account SID"
          value={String(g(draft, 'notifications.twilio_sid', '') || '')}
          onChange={(v) => setVal('notifications.twilio_sid', v)}
          placeholder="AC... (stored encrypted)"
        />
        <VaultKeyInput
          id="twilio_token"
          label="Twilio Auth Token"
          value={String(g(draft, 'notifications.twilio_token', '') || '')}
          onChange={(v) => setVal('notifications.twilio_token', v)}
          placeholder="Twilio auth token (stored encrypted)"
        />
        <TextInput
          label="WhatsApp From (Twilio number)"
          value={g(draft, 'notifications.whatsapp.from_number', '')}
          onChange={(v) => setVal('notifications.whatsapp.from_number', v)}
          placeholder="whatsapp:+14155238886"
        />
        <TextInput
          label="Your WhatsApp Number"
          value={g(draft, 'notifications.whatsapp.to_number', '')}
          onChange={(v) => setVal('notifications.whatsapp.to_number', v)}
          placeholder="whatsapp:+15551234567"
        />
      </div>

      {/* Telegram Channel */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Telegram</h2>
          <TestButton channel="telegram" label="Test" />
        </div>
        <Toggle
          label="Enable Telegram"
          value={g(draft, 'notifications.telegram.enabled', false)}
          onChange={(v) => setVal('notifications.telegram.enabled', v)}
        />
        <VaultKeyInput
          id="telegram_token"
          label="Bot Token"
          value={String(g(draft, 'notifications.telegram_token', '') || '')}
          onChange={(v) => setVal('notifications.telegram_token', v)}
          placeholder="Bot token from @BotFather (stored encrypted)"
        />
        <TextInput
          label="Chat ID"
          value={g(draft, 'notifications.telegram.chat_id', '')}
          onChange={(v) => setVal('notifications.telegram.chat_id', v)}
          placeholder="Numeric ID or @channel_name"
        />
      </div>

      {/* Email Channel */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Email (SMTP)</h2>
          <TestButton channel="email" label="Test" />
        </div>
        <Toggle
          label="Enable Email"
          value={g(draft, 'notifications.email.enabled', false)}
          onChange={(v) => setVal('notifications.email.enabled', v)}
        />
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <TextInput
            label="SMTP Host"
            value={g(draft, 'notifications.email.smtp_host', 'smtp.gmail.com')}
            onChange={(v) => setVal('notifications.email.smtp_host', v)}
            placeholder="smtp.gmail.com"
          />
          <NumberInput
            label="SMTP Port"
            value={g(draft, 'notifications.email.smtp_port', 587)}
            onChange={(v) => setVal('notifications.email.smtp_port', v)}
            step={1} min={1} max={65535}
          />
        </div>
        <TextInput
          label="SMTP Username"
          value={g(draft, 'notifications.email.username', '')}
          onChange={(v) => setVal('notifications.email.username', v)}
          placeholder="your@gmail.com"
        />
        <VaultKeyInput
          id="email_password"
          label="App Password"
          value={String(g(draft, 'notifications.email_password', '') || '')}
          onChange={(v) => setVal('notifications.email_password', v)}
          placeholder="Gmail App Password (not your main password)"
        />
        <TextInput
          label="From Address"
          value={g(draft, 'notifications.email.from_address', '')}
          onChange={(v) => setVal('notifications.email.from_address', v)}
          placeholder="nexustrader@gmail.com"
        />
        <TextInput
          label="To Address(es)"
          value={g(draft, 'notifications.email.to_addresses', '')}
          onChange={(v) => setVal('notifications.email.to_addresses', v)}
          placeholder="you@example.com (comma-separate multiple)"
        />
        <Toggle
          label="Use TLS"
          value={g(draft, 'notifications.email.use_tls', true)}
          onChange={(v) => setVal('notifications.email.use_tls', v)}
        />
      </div>

      {/* Gemini/Google Channel */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Gemini / Gmail</h2>
          <TestButton channel="gemini" label="Test" />
        </div>
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-sm text-blue-800">
          Requires Google 2FA enabled and a Gmail App Password (Settings &rarr; Security &rarr; App Passwords).
        </div>
        <Toggle
          label="Enable Gemini Channel"
          value={g(draft, 'notifications.gemini.enabled', false)}
          onChange={(v) => setVal('notifications.gemini.enabled', v)}
        />
        <TextInput
          label="Gmail Address"
          value={g(draft, 'notifications.gemini.username', '')}
          onChange={(v) => setVal('notifications.gemini.username', v)}
          placeholder="yourname@gmail.com"
        />
        <VaultKeyInput
          id="gemini_password"
          label="Gmail App Password"
          value={String(g(draft, 'notifications.gemini_password', '') || '')}
          onChange={(v) => setVal('notifications.gemini_password', v)}
          placeholder="16-character App Password (stored encrypted)"
        />
        <TextInput
          label="Deliver To (Gmail)"
          value={g(draft, 'notifications.gemini.to_address', '')}
          onChange={(v) => setVal('notifications.gemini.to_address', v)}
          placeholder="Same as Gmail Address (or another Gmail)"
        />
        <Toggle
          label="AI-Enrich Notifications (Gemini Flash)"
          value={g(draft, 'notifications.gemini.ai_enrich', false)}
          onChange={(v) => setVal('notifications.gemini.ai_enrich', v)}
        />
        <p className="text-xs text-gray-400">AI enrichment uses the Gemini API key configured in the AI & ML tab.</p>
      </div>

      {/* SMS Channel */}
      <div className="space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">SMS (Twilio)</h2>
          <TestButton channel="sms" label="Test" />
        </div>
        <Toggle
          label="Enable SMS"
          value={g(draft, 'notifications.sms.enabled', false)}
          onChange={(v) => setVal('notifications.sms.enabled', v)}
        />
        <TextInput
          label="SMS From (Twilio number)"
          value={g(draft, 'notifications.sms.from_number', '')}
          onChange={(v) => setVal('notifications.sms.from_number', v)}
          placeholder="+14155238886 (plain E.164 — no 'whatsapp:' prefix)"
        />
        <TextInput
          label="Your Phone Number"
          value={g(draft, 'notifications.sms.to_number', '')}
          onChange={(v) => setVal('notifications.sms.to_number', v)}
          placeholder="+15551234567"
        />
        <p className="text-xs text-gray-400">SMS uses the same Twilio credentials as WhatsApp (configured above).</p>
      </div>

      {/* Notification Preferences */}
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">What to Notify</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-1">
          <Toggle label="Trade Opened" value={g(draft, 'notifications.preferences.trade_opened', true)} onChange={(v) => setVal('notifications.preferences.trade_opened', v)} />
          <Toggle label="Trade Closed" value={g(draft, 'notifications.preferences.trade_closed', true)} onChange={(v) => setVal('notifications.preferences.trade_closed', v)} />
          <Toggle label="Stop-Loss Hit" value={g(draft, 'notifications.preferences.trade_stopped', true)} onChange={(v) => setVal('notifications.preferences.trade_stopped', v)} />
          <Toggle label="Signal Rejected" value={g(draft, 'notifications.preferences.trade_rejected', false)} onChange={(v) => setVal('notifications.preferences.trade_rejected', v)} />
          <Toggle label="Trade Modified" value={g(draft, 'notifications.preferences.trade_modified', false)} onChange={(v) => setVal('notifications.preferences.trade_modified', v)} />
          <Toggle label="Strategy Signal Alert" value={g(draft, 'notifications.preferences.strategy_signal', false)} onChange={(v) => setVal('notifications.preferences.strategy_signal', v)} />
          <Toggle label="Risk Warning" value={g(draft, 'notifications.preferences.risk_warning', true)} onChange={(v) => setVal('notifications.preferences.risk_warning', v)} />
          <Toggle label="Market / Regime Alert" value={g(draft, 'notifications.preferences.market_condition', false)} onChange={(v) => setVal('notifications.preferences.market_condition', v)} />
          <Toggle label="System Errors" value={g(draft, 'notifications.preferences.system_error', true)} onChange={(v) => setVal('notifications.preferences.system_error', v)} />
          <Toggle label="Emergency Stop" value={g(draft, 'notifications.preferences.emergency_stop', true)} onChange={(v) => setVal('notifications.preferences.emergency_stop', v)} />
          <Toggle label="Daily Summary" value={g(draft, 'notifications.preferences.daily_summary', true)} onChange={(v) => setVal('notifications.preferences.daily_summary', v)} />
        </div>
        <div className="flex items-center gap-3">
          <Toggle
            label="Health Check"
            value={g(draft, 'notifications.preferences.health_check', true)}
            onChange={(v) => setVal('notifications.preferences.health_check', v)}
          />
        </div>
        <SelectInput
          label="Health Check Interval"
          value={String(g(draft, 'notifications.preferences.health_check_interval_hours', 6))}
          options={healthCheckIntervals}
          onChange={(v) => setVal('notifications.preferences.health_check_interval_hours', parseInt(v, 10))}
        />
      </div>

      {/* Test All Channels */}
      <div className="pt-2 border-t border-gray-100">
        <button
          onClick={handleTestAll}
          disabled={testAllLoading}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium min-h-[44px] transition-colors',
            testAllLoading ? 'bg-gray-100 text-gray-400' : 'bg-indigo-50 text-indigo-700 hover:bg-indigo-100',
          )}
        >
          {testAllLoading ? <Loader2 className="w-4 h-4 animate-spin" /> : <Send className="w-4 h-4" />}
          Test All Channels
        </button>
      </div>
    </div>
  );
}

// ── Backtesting Tab ─────────────────────────────────────────

function BacktestingTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  return (
    <div className="space-y-4">
      <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Backtesting Defaults</h2>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <NumberInput
          label="Default Trading Fee (%)"
          value={g(draft, 'backtesting.default_fee_pct', 0.1)}
          onChange={(v) => setVal('backtesting.default_fee_pct', v)}
          step={0.01} min={0} max={1}
        />
        <NumberInput
          label="Default Slippage (%)"
          value={g(draft, 'backtesting.default_slippage_pct', 0.05)}
          onChange={(v) => setVal('backtesting.default_slippage_pct', v)}
          step={0.01} min={0} max={1}
        />
        <NumberInput
          label="Default Initial Capital (USDT)"
          value={g(draft, 'backtesting.default_initial_capital', 10000)}
          onChange={(v) => setVal('backtesting.default_initial_capital', v)}
          step={1000} min={100} max={10000000}
        />
        <NumberInput
          label="Walk-Forward Train Window (months)"
          value={g(draft, 'backtesting.walk_forward_train_months', 24)}
          onChange={(v) => setVal('backtesting.walk_forward_train_months', v)}
          step={1} min={1} max={60}
        />
        <NumberInput
          label="Walk-Forward Validate Window (months)"
          value={g(draft, 'backtesting.walk_forward_validate_months', 6)}
          onChange={(v) => setVal('backtesting.walk_forward_validate_months', v)}
          step={1} min={1} max={24}
        />
      </div>
    </div>
  );
}

// ── Intelligence Agents Tab ─────────────────────────────────

function AgentsTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Agent Behaviour</h2>
        <Toggle
          label="Auto-start agents on exchange connect"
          value={g(draft, 'agents.auto_start', true)}
          onChange={(v) => setVal('agents.auto_start', v)}
        />
        <NumberInput
          label="Agent confluence boost threshold"
          value={g(draft, 'agents.min_confluence_boost', 0.25)}
          onChange={(v) => setVal('agents.min_confluence_boost', v)}
          step={0.05} min={0} max={1}
        />
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Liquidation Intelligence</h2>
        <VaultKeyInput
          id="coinglass_key"
          label="Coinglass API Key"
          value={String(g(draft, 'agents.coinglass_api_key', '') || '')}
          onChange={(v) => setVal('agents.coinglass_api_key', v)}
          placeholder="Coinglass API key (stored encrypted)"
        />
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Macro Intelligence</h2>
        <VaultKeyInput
          id="fred_key"
          label="FRED API Key"
          value={String(g(draft, 'agents.fred_api_key', '') || '')}
          onChange={(v) => setVal('agents.fred_api_key', v)}
          placeholder="Federal Reserve FRED key (stored encrypted)"
        />
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Social Sentiment</h2>
        <VaultKeyInput
          id="lunarcrush_key"
          label="LunarCrush API Key"
          value={String(g(draft, 'agents.lunarcrush_api_key', '') || '')}
          onChange={(v) => setVal('agents.lunarcrush_api_key', v)}
          placeholder="LunarCrush API key (stored encrypted)"
        />
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Options Flow (BTC/ETH only)</h2>
        <Toggle
          label="Enable Deribit options data"
          value={g(draft, 'agents.options_enabled', true)}
          onChange={(v) => setVal('agents.options_enabled', v)}
        />
        <NumberInput
          label="Max days to expiry"
          value={g(draft, 'agents.options_max_days_expiry', 35)}
          onChange={(v) => setVal('agents.options_max_days_expiry', v)}
          step={5} min={1} max={90}
        />
      </div>

      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Funding Rate & Order Book</h2>
        <Toggle
          label="Enable funding rate monitoring"
          value={g(draft, 'agents.funding_enabled', true)}
          onChange={(v) => setVal('agents.funding_enabled', v)}
        />
        <Toggle
          label="Enable order book monitoring"
          value={g(draft, 'agents.orderbook_enabled', true)}
          onChange={(v) => setVal('agents.orderbook_enabled', v)}
        />
      </div>
    </div>
  );
}

// ── Portfolio Allocation Tab ────────────────────────────────

function PortfolioAllocationTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const symbols = ['BTC/USDT', 'ETH/USDT', 'SOL/USDT', 'BNB/USDT', 'XRP/USDT'];
  const mode = g(draft, 'symbol_allocation.mode', 'STATIC');

  const WeightGrid = ({ prefix, label }: { prefix: string; label: string }) => (
    <div className="space-y-3">
      <p className="text-xs font-medium text-gray-500 uppercase">{label}</p>
      <div className="grid grid-cols-5 gap-2">
        {symbols.map((sym) => (
          <div key={sym}>
            <label className="block text-xs text-gray-500 mb-0.5">{sym.split('/')[0]}</label>
            <input
              type="number"
              value={g(draft, `${prefix}.${sym}`, 1.0)}
              onChange={(e) => setVal(`${prefix}.${sym}`, parseFloat(e.target.value) || 0)}
              step={0.1}
              min={0}
              max={3}
              className="w-full px-2 py-1.5 border border-gray-300 rounded text-sm text-center min-h-[36px] focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        ))}
      </div>
    </div>
  );

  return (
    <div className="space-y-6">
      <div className="space-y-4">
        <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Portfolio Allocation</h2>
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-3 text-sm text-blue-800">
          Symbol weights adjust candidate ranking only. They never modify signals, sizing, or risk parameters.
          Study 4 baseline: SOL=1.3, ETH=1.2, BTC=1.0, BNB=0.8, XRP=0.8.
        </div>
        <SelectInput
          label="Allocation Mode"
          value={mode}
          options={['STATIC', 'DYNAMIC']}
          onChange={(v) => setVal('symbol_allocation.mode', v)}
        />
      </div>

      {mode === 'STATIC' && (
        <WeightGrid
          prefix="symbol_allocation.static_weights"
          label="Static Weights"
        />
      )}

      {mode === 'DYNAMIC' && (
        <>
          <div className="space-y-4">
            <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">BTC Dominance Thresholds</h2>
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
              <NumberInput
                label="Current BTC Dominance %"
                value={g(draft, 'symbol_allocation.btc_dominance_pct', 50.0)}
                onChange={(v) => setVal('symbol_allocation.btc_dominance_pct', v)}
                step={1} min={0} max={100}
              />
              <NumberInput
                label="High threshold (BTC Dominant)"
                value={g(draft, 'symbol_allocation.btc_dominance_high', 55.0)}
                onChange={(v) => setVal('symbol_allocation.btc_dominance_high', v)}
                step={1} min={30} max={80}
              />
              <NumberInput
                label="Low threshold (Alt Season)"
                value={g(draft, 'symbol_allocation.btc_dominance_low', 45.0)}
                onChange={(v) => setVal('symbol_allocation.btc_dominance_low', v)}
                step={1} min={20} max={70}
              />
            </div>
          </div>

          <WeightGrid
            prefix="symbol_allocation.profiles.btc_dominant"
            label="BTC Dominant Profile (dominance > high threshold)"
          />
          <WeightGrid
            prefix="symbol_allocation.profiles.neutral"
            label="Neutral Profile (between thresholds)"
          />
          <WeightGrid
            prefix="symbol_allocation.profiles.alt_season"
            label="Alt Season Profile (dominance < low threshold)"
          />
        </>
      )}
    </div>
  );
}

function APIKeysTab({ draft, setVal, get: g }: { draft: any; setVal: any; get: any }) {
  const [reveal, setReveal] = useState<Record<string, boolean>>({});
  const keys = [
    { id: 'cryptopanic_api_key', label: 'CryptoPanic API Key', path: 'agents.cryptopanic_api_key' },
    { id: 'coinglass_api_key', label: 'Coinglass API Key', path: 'agents.coinglass_api_key' },
    { id: 'reddit_client_id', label: 'Reddit Client ID', path: 'sentiment.reddit_client_id' },
    { id: 'reddit_client_secret', label: 'Reddit Client Secret', path: 'sentiment.reddit_client_secret' },
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
