/**
 * Phase 8B: Exchange Management Page
 *
 * Desktop parity with exchange_page.py (PySide6).
 * Two tabs: Exchanges (CRUD + connection test) and Asset Management.
 */
import { useState } from 'react';
import { useQuery, useQueryClient, useMutation } from '@tanstack/react-query';
import {
  Plug, Plus, Pencil, Trash2, Power, PowerOff, RefreshCw,
  Key, Shield, Eye, EyeOff, X, CheckCircle2, AlertCircle,
  Search, Coins, Loader2,
} from 'lucide-react';
import {
  getExchanges, getSupportedExchanges, createExchange, updateExchange,
  deleteExchange, activateExchange, deactivateExchange, testConnection,
  getExchangeAssets, syncExchangeAssets,
  type ExchangeConfig, type SupportedExchange,
  type ConnectionTestResult,
} from '../api/exchanges';
import { cn } from '../lib/utils';

type Tab = 'exchanges' | 'assets';
type Mode = 'live' | 'sandbox' | 'demo';

// ── Shared Components ─────────────────────────────────────

function Badge({ children, variant }: { children: React.ReactNode; variant: 'green' | 'gray' | 'red' | 'yellow' | 'blue' }) {
  const colors = {
    green: 'bg-green-100 text-green-700 border-green-200',
    gray: 'bg-gray-100 text-gray-500 border-gray-200',
    red: 'bg-red-100 text-red-700 border-red-200',
    yellow: 'bg-amber-100 text-amber-700 border-amber-200',
    blue: 'bg-blue-100 text-blue-700 border-blue-200',
  };
  return (
    <span className={cn('px-2 py-0.5 rounded text-xs font-medium border', colors[variant])}>
      {children}
    </span>
  );
}

function ModeBadge({ mode }: { mode: string }) {
  if (mode === 'demo') return <Badge variant="blue">DEMO</Badge>;
  if (mode === 'sandbox') return <Badge variant="yellow">TESTNET</Badge>;
  return <Badge variant="red">LIVE</Badge>;
}

// ── Main Page ─────────────────────────────────────────────

export default function ExchangeManagement() {
  const [activeTab, setActiveTab] = useState<Tab>('exchanges');

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Plug className="w-5 h-5 text-gray-400" />
          <div>
            <h1 className="text-xl font-semibold text-gray-900">Exchange Management</h1>
            <p className="text-sm text-gray-500">Configure exchange connections and API credentials</p>
          </div>
        </div>
      </div>

      {/* Tab Bar */}
      <div className="flex gap-1 border-b border-gray-200">
        {([
          { key: 'exchanges' as Tab, label: 'Exchanges', icon: Plug },
          { key: 'assets' as Tab, label: 'Asset Management', icon: Coins },
        ]).map((t) => (
          <button
            key={t.key}
            onClick={() => setActiveTab(t.key)}
            className={cn(
              'flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px',
              activeTab === t.key
                ? 'border-blue-600 text-blue-700'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300',
            )}
          >
            <t.icon className="w-4 h-4" />
            {t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      {activeTab === 'exchanges' && <ExchangesTab />}
      {activeTab === 'assets' && <AssetsTab />}
    </div>
  );
}

// ── Exchanges Tab ─────────────────────────────────────────

function ExchangesTab() {
  const queryClient = useQueryClient();
  const [showDialog, setShowDialog] = useState(false);
  const [editingExchange, setEditingExchange] = useState<ExchangeConfig | null>(null);
  const [deleteConfirm, setDeleteConfirm] = useState<number | null>(null);

  const { data: exchanges = [], isLoading } = useQuery({
    queryKey: ['exchanges'],
    queryFn: getExchanges,
  });

  const { data: supported = [] } = useQuery({
    queryKey: ['supported-exchanges'],
    queryFn: getSupportedExchanges,
  });

  const activateMut = useMutation({
    mutationFn: activateExchange,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['exchanges'] }),
  });

  const deactivateMut = useMutation({
    mutationFn: deactivateExchange,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['exchanges'] }),
  });

  const deleteMut = useMutation({
    mutationFn: deleteExchange,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['exchanges'] });
      setDeleteConfirm(null);
    },
  });

  const handleEdit = (ex: ExchangeConfig) => {
    setEditingExchange(ex);
    setShowDialog(true);
  };

  const handleAdd = () => {
    setEditingExchange(null);
    setShowDialog(true);
  };

  const handleDialogClose = () => {
    setShowDialog(false);
    setEditingExchange(null);
    queryClient.invalidateQueries({ queryKey: ['exchanges'] });
  };

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
      {/* Left: Exchange Cards */}
      <div className="lg:col-span-2 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium text-gray-500 uppercase tracking-wide">Configured Exchanges</h2>
          <button
            onClick={handleAdd}
            className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors min-h-[36px]"
          >
            <Plus className="w-4 h-4" /> Add Exchange
          </button>
        </div>

        {isLoading ? (
          <div className="flex items-center justify-center h-32 text-gray-400">
            <Loader2 className="w-5 h-5 animate-spin mr-2" /> Loading...
          </div>
        ) : exchanges.length === 0 ? (
          <div className="bg-white border border-gray-200 rounded-lg p-8 text-center text-gray-400">
            <Plug className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p>No exchanges configured yet.</p>
            <p className="text-sm">Click "+ Add Exchange" to get started.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {exchanges.map((ex) => (
              <div key={ex.id} className="bg-white border border-gray-200 rounded-lg p-4">
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span className="font-medium text-gray-900">{ex.name}</span>
                    <Badge variant={ex.is_active ? 'green' : 'gray'}>
                      {ex.is_active ? 'ACTIVE' : 'INACTIVE'}
                    </Badge>
                    <ModeBadge mode={ex.mode} />
                  </div>
                </div>
                <div className="flex items-center gap-4 text-sm text-gray-500 mb-3">
                  <span>{ex.exchange_id}</span>
                  <span className="flex items-center gap-1">
                    {ex.has_api_key ? (
                      <><Key className="w-3.5 h-3.5" /> API Keys Configured</>
                    ) : (
                      <><AlertCircle className="w-3.5 h-3.5 text-amber-500" /> No API Keys</>
                    )}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={() => handleEdit(ex)} className="px-3 py-1.5 text-sm text-gray-600 hover:bg-gray-100 rounded-md transition-colors">
                    <Pencil className="w-3.5 h-3.5 inline mr-1" /> Edit
                  </button>
                  {deleteConfirm === ex.id ? (
                    <div className="flex items-center gap-1">
                      <button onClick={() => deleteMut.mutate(ex.id)} className="px-3 py-1.5 text-sm text-red-600 bg-red-50 hover:bg-red-100 rounded-md transition-colors">
                        Confirm Delete
                      </button>
                      <button onClick={() => setDeleteConfirm(null)} className="px-3 py-1.5 text-sm text-gray-500 hover:bg-gray-100 rounded-md transition-colors">
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <button
                      onClick={() => setDeleteConfirm(ex.id)}
                      disabled={ex.is_active}
                      className={cn(
                        'px-3 py-1.5 text-sm rounded-md transition-colors',
                        ex.is_active ? 'text-gray-300 cursor-not-allowed' : 'text-red-500 hover:bg-red-50',
                      )}
                    >
                      <Trash2 className="w-3.5 h-3.5 inline mr-1" /> Remove
                    </button>
                  )}
                  <div className="ml-auto">
                    {ex.is_active ? (
                      <button
                        onClick={() => deactivateMut.mutate(ex.id)}
                        disabled={deactivateMut.isPending}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-amber-600 border border-amber-300 rounded-md hover:bg-amber-50 transition-colors"
                      >
                        <PowerOff className="w-3.5 h-3.5" /> Deactivate
                      </button>
                    ) : (
                      <button
                        onClick={() => activateMut.mutate(ex.id)}
                        disabled={activateMut.isPending}
                        className="flex items-center gap-1.5 px-3 py-1.5 text-sm text-green-600 border border-green-300 rounded-md hover:bg-green-50 transition-colors"
                      >
                        <Power className="w-3.5 h-3.5" /> Set Active
                      </button>
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Right: Info Panel */}
      <div className="space-y-4">
        {/* Supported Exchanges */}
        <div className="bg-white border border-gray-200 rounded-lg p-4">
          <h3 className="text-sm font-medium text-gray-500 uppercase tracking-wide mb-3">Supported Exchanges</h3>
          <div className="space-y-2">
            {supported.map((s) => (
              <div key={s.exchange_id} className="flex items-center justify-between py-1.5 px-2 rounded-md hover:bg-gray-50">
                <span className="text-sm font-medium text-gray-900">{s.name}</span>
                <span className="text-xs text-gray-400">{s.exchange_id}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Security Info */}
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <h3 className="flex items-center gap-2 text-sm font-medium text-blue-800 mb-2">
            <Shield className="w-4 h-4" /> Security
          </h3>
          <ul className="space-y-1.5 text-xs text-blue-700">
            <li>All API keys encrypted with AES-256 (Fernet)</li>
            <li>Keys stored locally — never transmitted to third parties</li>
            <li>Use Read + Trade permissions only</li>
            <li>Never grant Withdrawal permissions</li>
            <li>Enable IP whitelist on your exchange</li>
          </ul>
        </div>
      </div>

      {/* Add/Edit Dialog */}
      {showDialog && (
        <ExchangeDialog
          exchange={editingExchange}
          supported={supported}
          onClose={handleDialogClose}
        />
      )}
    </div>
  );
}

// ── Add/Edit Exchange Dialog ──────────────────────────────

function ExchangeDialog({
  exchange,
  supported,
  onClose,
}: {
  exchange: ExchangeConfig | null;
  supported: SupportedExchange[];
  onClose: () => void;
}) {
  const isEdit = !!exchange;
  const queryClient = useQueryClient();

  const [exchangeId, setExchangeId] = useState(exchange?.exchange_id || (supported[0]?.exchange_id ?? ''));
  const [mode, setMode] = useState<Mode>((exchange?.mode as Mode) || 'live');
  const [apiKey, setApiKey] = useState('');
  const [apiSecret, setApiSecret] = useState('');
  const [passphrase, setPassphrase] = useState('');
  const [showSecret, setShowSecret] = useState(false);
  const [testStatus, setTestStatus] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
  const [testMessage, setTestMessage] = useState('');
  const [saving, setSaving] = useState(false);

  const selectedInfo = supported.find((s) => s.exchange_id === exchangeId);

  const handleTest = async () => {
    setTestStatus('testing');
    setTestMessage('Testing...');
    try {
      const result: ConnectionTestResult = await testConnection({
        exchange_id: exchangeId,
        api_key: apiKey || undefined,
        api_secret: apiSecret || undefined,
        passphrase: passphrase || undefined,
        mode,
      });
      if (result.status === 'ok' || result.status === 'success') {
        const parts = ['Connected'];
        if (result.mode_label) parts[0] += ` [${result.mode_label}]`;
        parts[0] += ' \u2713';
        if (result.markets) parts.push(`${result.markets} markets`);
        if (result.balance_usdt !== undefined) parts.push(`USDT balance: ${result.balance_usdt.toFixed(2)}`);
        setTestMessage(parts.join(' | '));
        setTestStatus('success');
      } else {
        setTestMessage(result.error || result.message || 'Connection failed');
        setTestStatus('error');
      }
    } catch (err: any) {
      setTestMessage(err?.response?.data?.detail || err?.message || 'Connection test failed');
      setTestStatus('error');
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      if (isEdit && exchange) {
        await updateExchange(exchange.id, {
          api_key: apiKey || undefined,
          api_secret: apiSecret || undefined,
          passphrase: passphrase || undefined,
          mode,
        });
      } else {
        const info = supported.find((s) => s.exchange_id === exchangeId);
        await createExchange({
          name: info?.name || exchangeId,
          exchange_id: exchangeId,
          api_key: apiKey || undefined,
          api_secret: apiSecret || undefined,
          passphrase: passphrase || undefined,
          mode,
        });
      }
      queryClient.invalidateQueries({ queryKey: ['exchanges'] });
      onClose();
    } catch (err: any) {
      setTestMessage(err?.response?.data?.detail || 'Save failed');
      setTestStatus('error');
    } finally {
      setSaving(false);
    }
  };

  // Mode info text
  const modeInfo = {
    live: 'API credentials are encrypted with AES-256 (Fernet) and stored locally. They never leave your machine. Use read+trade permissions only — never withdrawal.',
    sandbox: 'Sandbox / Testnet mode — uses exchange testnet endpoints. Supported on Binance, Bybit, OKX only. Create testnet API keys from your exchange\'s developer portal.',
    demo: 'Demo Trading mode — uses api-demo.bybit.com. Create Demo Trading API keys at demo.bybit.com (not the main site). Paper money only — no real funds involved. Bybit only.',
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40" onClick={onClose}>
      <div
        className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
          <h2 className="text-lg font-semibold text-gray-900">
            {isEdit ? 'Edit Exchange' : 'Add Exchange'}
          </h2>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Form */}
        <div className="px-6 py-4 space-y-4">
          <p className="text-sm font-medium text-gray-700">Configure Exchange Connection</p>

          {/* Exchange Selector */}
          <div>
            <label className="block text-sm text-gray-600 mb-1">Exchange</label>
            <select
              value={exchangeId}
              onChange={(e) => setExchangeId(e.target.value)}
              disabled={isEdit}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
            >
              {supported.map((s) => (
                <option key={s.exchange_id} value={s.exchange_id}>{s.name}</option>
              ))}
            </select>
          </div>

          {/* Mode Selector */}
          <div>
            <label className="block text-sm text-gray-600 mb-1">Mode</label>
            <select
              value={mode}
              onChange={(e) => setMode(e.target.value as Mode)}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="live">Live (real funds)</option>
              {selectedInfo?.has_sandbox && <option value="sandbox">Sandbox / Testnet</option>}
              {selectedInfo?.has_demo && <option value="demo">Demo Trading (Bybit only)</option>}
            </select>
          </div>

          {/* API Key */}
          <div>
            <label className="block text-sm text-gray-600 mb-1">API Key</label>
            <input
              type="text"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={isEdit && exchange?.has_api_key ? exchange.api_key_masked : 'Paste your API Key here'}
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {/* API Secret */}
          <div>
            <label className="block text-sm text-gray-600 mb-1">API Secret</label>
            <div className="flex gap-2">
              <input
                type={showSecret ? 'text' : 'password'}
                value={apiSecret}
                onChange={(e) => setApiSecret(e.target.value)}
                placeholder={isEdit && exchange?.has_api_secret ? '••••••••' : 'Paste your API Secret here'}
                className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                onClick={() => setShowSecret(!showSecret)}
                className="px-3 py-2 rounded-lg border border-gray-300 text-gray-500 hover:bg-gray-50 min-h-[44px]"
              >
                {showSecret ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
              </button>
            </div>
          </div>

          {/* Passphrase (conditional) */}
          {selectedInfo?.needs_passphrase && (
            <div>
              <label className="block text-sm text-gray-600 mb-1">Passphrase</label>
              <input
                type="password"
                value={passphrase}
                onChange={(e) => setPassphrase(e.target.value)}
                placeholder={isEdit && exchange?.has_passphrase ? '••••••••' : 'KuCoin/OKX only — leave blank for others'}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm font-mono min-h-[44px] focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          )}

          {/* Mode Info Box */}
          <div className={cn(
            'rounded-lg p-3 text-sm',
            mode === 'live' ? 'bg-blue-50 text-blue-800 border border-blue-200' :
            mode === 'sandbox' ? 'bg-amber-50 text-amber-800 border border-amber-200' :
            'bg-sky-50 text-sky-800 border border-sky-200'
          )}>
            <Shield className="w-4 h-4 inline mr-1" />
            {modeInfo[mode]}
          </div>

          {/* Connection Test */}
          <div className="flex items-center gap-3">
            <button
              onClick={handleTest}
              disabled={testStatus === 'testing'}
              className="flex items-center gap-2 px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 transition-colors min-h-[36px]"
            >
              {testStatus === 'testing' ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <RefreshCw className="w-4 h-4" />
              )}
              Test Connection
            </button>
            {testMessage && (
              <span className={cn(
                'text-xs',
                testStatus === 'success' ? 'text-green-600' :
                testStatus === 'error' ? 'text-red-600' :
                'text-gray-500',
              )}>
                {testStatus === 'success' && <CheckCircle2 className="w-3.5 h-3.5 inline mr-1" />}
                {testStatus === 'error' && <AlertCircle className="w-3.5 h-3.5 inline mr-1" />}
                {testMessage}
              </span>
            )}
          </div>
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-gray-200">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:bg-gray-100 rounded-lg transition-colors min-h-[36px]">
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            className="flex items-center gap-2 px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors min-h-[36px]"
          >
            {saving && <Loader2 className="w-4 h-4 animate-spin" />}
            {isEdit ? 'Save Changes' : 'Save Exchange'}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Asset Management Tab ──────────────────────────────────

function AssetsTab() {
  const [quote, setQuote] = useState('USDT');
  const [search, setSearch] = useState('');
  const [syncStatus, setSyncStatus] = useState('');
  const [syncing, setSyncing] = useState(false);

  const { data: exchanges = [] } = useQuery({
    queryKey: ['exchanges'],
    queryFn: getExchanges,
  });

  const activeExchange = exchanges.find((e) => e.is_active);

  const { data: assetsData, isLoading, refetch } = useQuery({
    queryKey: ['exchange-assets', activeExchange?.id, quote, search],
    queryFn: () => activeExchange ? getExchangeAssets(activeExchange.id, { quote, search: search || undefined }) : Promise.resolve({ assets: [], count: 0 }),
    enabled: !!activeExchange,
  });

  const assets = assetsData?.assets ?? [];

  const handleSync = async () => {
    if (!activeExchange) return;
    setSyncing(true);
    setSyncStatus('Syncing...');
    try {
      const result = await syncExchangeAssets(activeExchange.id);
      setSyncStatus(`\u2713 ${result.new_count ?? 0} new assets synced`);
      refetch();
    } catch (err: any) {
      setSyncStatus(`Error: ${err?.response?.data?.detail || err?.message || 'Sync failed'}`);
    } finally {
      setSyncing(false);
      setTimeout(() => setSyncStatus(''), 5000);
    }
  };

  if (!activeExchange) {
    return (
      <div className="bg-white border border-gray-200 rounded-lg p-8 text-center text-gray-400">
        <Coins className="w-8 h-8 mx-auto mb-2 opacity-50" />
        <p>No active exchange.</p>
        <p className="text-sm">Activate an exchange in the Exchanges tab to manage assets.</p>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2">
          <label className="text-sm text-gray-600">Quote:</label>
          <select
            value={quote}
            onChange={(e) => setQuote(e.target.value)}
            className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm min-h-[36px] focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            {['USDT', 'BTC', 'ETH', 'BNB'].map((q) => (
              <option key={q} value={q}>{q}</option>
            ))}
          </select>
        </div>
        <div className="relative">
          <Search className="w-4 h-4 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search symbol..."
            className="pl-9 pr-3 py-1.5 border border-gray-300 rounded-lg text-sm w-40 min-h-[36px] focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="flex items-center gap-2 px-4 py-1.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 transition-colors min-h-[36px]"
        >
          {syncing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
          Sync Assets from Exchange
        </button>
        {syncStatus && (
          <span className={cn('text-xs', syncStatus.startsWith('\u2713') ? 'text-green-600' : syncStatus.startsWith('Error') ? 'text-red-500' : 'text-gray-500')}>
            {syncStatus}
          </span>
        )}
      </div>

      {/* Asset Table */}
      <div className="bg-white border border-gray-200 rounded-lg overflow-hidden">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="bg-gray-50 border-b border-gray-200">
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Symbol</th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Base</th>
                <th className="text-left px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Quote</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Price Precision</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Min Amount</th>
                <th className="text-right px-4 py-3 text-xs font-medium text-gray-500 uppercase tracking-wide">Min Cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {isLoading ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-gray-400">
                    <Loader2 className="w-5 h-5 animate-spin inline mr-2" /> Loading assets...
                  </td>
                </tr>
              ) : assets.length === 0 ? (
                <tr>
                  <td colSpan={6} className="px-4 py-8 text-center text-gray-400">
                    No assets found. Click "Sync Assets from Exchange" to fetch.
                  </td>
                </tr>
              ) : (
                assets.map((a) => (
                  <tr key={a.id} className="hover:bg-gray-50 transition-colors">
                    <td className="px-4 py-2.5 font-medium text-gray-900">{a.symbol}</td>
                    <td className="px-4 py-2.5 text-gray-600">{a.base_currency}</td>
                    <td className="px-4 py-2.5 text-gray-600">{a.quote_currency}</td>
                    <td className="px-4 py-2.5 text-right text-gray-600">{a.price_precision}</td>
                    <td className="px-4 py-2.5 text-right text-gray-600">{a.min_amount ?? '—'}</td>
                    <td className="px-4 py-2.5 text-right text-gray-600">{a.min_cost ?? '—'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
        <div className="px-4 py-2 border-t border-gray-100 text-xs text-gray-400">
          {assets.length} assets
        </div>
      </div>
    </div>
  );
}
