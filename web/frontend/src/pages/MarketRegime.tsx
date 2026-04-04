import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getCurrentRegime, getRegimeHistory } from '../api/analytics';
import { formatPct, timeAgo } from '../lib/utils';

const REGIME_COLORS: Record<string, string> = {
  bull_trend: '#16a34a',
  bear_trend: '#dc2626',
  ranging: '#ca8a04',
  vol_expansion: '#7c3aed',
  vol_compression: '#8b5cf6',
  accumulation: '#22c55e',
  distribution: '#f87171',
  uncertain: '#6b7280',
};

const REGIME_LABELS: Record<string, string> = {
  bull_trend: 'Bull Trend',
  bear_trend: 'Bear Trend',
  ranging: 'Ranging',
  vol_expansion: 'Vol Expansion',
  vol_compression: 'Vol Compression',
  accumulation: 'Accumulation',
  distribution: 'Distribution',
  uncertain: 'Uncertain',
};

function CurrentRegimeCard({ regime, isLoading, isError }: {
  regime: any;
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <div className="animate-pulse space-y-4">
          <div className="h-8 bg-gray-200 rounded w-1/3" />
          <div className="h-6 bg-gray-200 rounded w-1/2" />
          <div className="h-4 bg-gray-200 rounded w-full" />
        </div>
      </div>
    );
  }

  if (isError || !regime) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-sm text-red-600">Failed to load regime data</p>
      </div>
    );
  }

  const color = REGIME_COLORS[regime.regime] || '#6b7280';

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <div className="flex items-start justify-between mb-6">
        <div>
          <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-2">
            Current Market Regime
          </p>
          <div className="flex items-center gap-3 mb-4">
            <div
              className="w-4 h-4 rounded-full"
              style={{ backgroundColor: color }}
            />
            <h2 className="text-3xl font-bold text-gray-900">
              {REGIME_LABELS[regime.regime] || regime.regime}
            </h2>
          </div>
        </div>
        <div className="text-right">
          <p className="text-sm text-gray-600">Confidence</p>
          <p className="text-2xl font-semibold text-gray-900">
            {formatPct(regime.confidence)}
          </p>
        </div>
      </div>

      {/* Confidence Bar */}
      <div className="mb-6">
        <div className="flex items-center justify-between mb-2">
          <p className="text-xs font-medium text-gray-600">Confidence Level</p>
          <p className="text-xs text-gray-400">{Math.round(regime.confidence * 100)}%</p>
        </div>
        <div className="w-full bg-gray-200 rounded-full h-2">
          <div
            className="h-2 rounded-full transition-all duration-300"
            style={{
              width: `${regime.confidence * 100}%`,
              backgroundColor: color,
            }}
          />
        </div>
      </div>

      {/* Classifier & Description */}
      <div className="grid grid-cols-2 gap-4 mb-6 pb-6 border-b border-gray-200">
        <div>
          <p className="text-xs font-medium text-gray-600 mb-1">Classifier</p>
          <p className="text-sm text-gray-900 font-medium">{regime.classifier || 'Unknown'}</p>
          {regime.hmm_fitted !== undefined && (
            <p className="text-xs text-gray-400 mt-1">
              HMM Fitted: {regime.hmm_fitted ? 'Yes' : 'No'}
            </p>
          )}
        </div>
        <div>
          <p className="text-xs font-medium text-gray-600 mb-1">Source</p>
          <p className="text-sm text-gray-900 font-medium">{regime.source || 'Unknown'}</p>
        </div>
      </div>

      {/* Description & Strategy Hints */}
      {regime.description && (
        <div className="mb-4">
          <p className="text-xs font-medium text-gray-600 mb-1">Description</p>
          <p className="text-sm text-gray-700">{regime.description}</p>
        </div>
      )}

      {regime.strategies && regime.strategies.length > 0 && (
        <div className="mb-4">
          <p className="text-xs font-medium text-gray-600 mb-2">Recommended Strategies</p>
          <div className="flex flex-wrap gap-2">
            {regime.strategies.map((strategy: string, idx: number) => (
              <span
                key={idx}
                className="px-3 py-1 rounded-full text-xs font-medium bg-blue-50 text-blue-700"
              >
                {strategy}
              </span>
            ))}
          </div>
        </div>
      )}

      {regime.risk_adjustment && (
        <div>
          <p className="text-xs font-medium text-gray-600 mb-1">Risk Adjustment</p>
          <p className="text-sm text-gray-700">{regime.risk_adjustment}</p>
        </div>
      )}
    </div>
  );
}

function HMMProbabilityDistribution({ regime, isLoading, isError }: {
  regime: any;
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading || isError || !regime?.probabilities) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
          HMM Probability Distribution
        </p>
        {isLoading && <div className="text-sm text-gray-400">Loading...</div>}
        {isError && <div className="text-sm text-red-600">Failed to load probabilities</div>}
        {!regime?.probabilities && <div className="text-sm text-gray-400">No probability data</div>}
      </div>
    );
  }

  const probs = regime.probabilities;
  const regimeKeys = Object.keys(probs).sort();

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
        HMM Probability Distribution
      </p>
      <div className="space-y-3">
        {regimeKeys.map((key: string) => {
          const prob = probs[key];
          const color = REGIME_COLORS[key] || '#6b7280';
          const pctStr = (prob * 100).toFixed(1);

          return (
            <div key={key} className="space-y-1">
              <div className="flex items-center justify-between text-xs">
                <span className="font-medium text-gray-700">
                  {REGIME_LABELS[key] || key}
                </span>
                <span className="font-mono text-gray-600">{pctStr}%</span>
              </div>
              <div className="w-full bg-gray-100 rounded-full h-2">
                <div
                  className="h-2 rounded-full transition-all duration-300"
                  style={{
                    width: `${prob * 100}%`,
                    backgroundColor: color,
                  }}
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RegimeStatistics({ history, isLoading, isError }: {
  history: any[];
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading || isError || !history) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
          Regime Distribution
        </p>
        {isLoading && <div className="text-sm text-gray-400">Loading...</div>}
        {isError && <div className="text-sm text-red-600">Failed to load history</div>}
      </div>
    );
  }

  // Count regimes in history
  const regimeCounts: Record<string, number> = {};
  history.forEach((entry) => {
    regimeCounts[entry.regime] = (regimeCounts[entry.regime] || 0) + 1;
  });

  const sorted = Object.entries(regimeCounts)
    .sort(([, a], [, b]) => b - a)
    .slice(0, 8);

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
        Regime Distribution
      </p>
      <div className="grid grid-cols-2 gap-3">
        {sorted.map(([regime, count]) => {
          const color = REGIME_COLORS[regime] || '#6b7280';
          return (
            <div
              key={regime}
              className="bg-gray-50 rounded-lg p-3 border border-gray-200"
            >
              <div className="flex items-center gap-2 mb-1">
                <div
                  className="w-2.5 h-2.5 rounded-full"
                  style={{ backgroundColor: color }}
                />
                <p className="text-xs font-medium text-gray-700">
                  {REGIME_LABELS[regime] || regime}
                </p>
              </div>
              <p className="text-lg font-semibold text-gray-900">{count}</p>
              <p className="text-xs text-gray-500 mt-0.5">
                {((count / history.length) * 100).toFixed(1)}%
              </p>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RegimeHistoryTable({ history, isLoading, isError }: {
  history: any[];
  isLoading: boolean;
  isError: boolean;
}) {
  if (isLoading) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
          Regime History
        </p>
        <div className="space-y-2">
          {[...Array(5)].map((_, i) => (
            <div key={i} className="h-10 bg-gray-100 rounded animate-pulse" />
          ))}
        </div>
      </div>
    );
  }

  if (isError || !history) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
          Regime History
        </p>
        <p className="text-sm text-red-600">Failed to load history</p>
      </div>
    );
  }

  if (history.length === 0) {
    return (
      <div className="bg-white rounded-lg border border-gray-200 p-6">
        <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
          Regime History
        </p>
        <p className="text-sm text-gray-400">No regime history available</p>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg border border-gray-200 p-6">
      <p className="text-xs font-medium text-gray-500 uppercase tracking-wide mb-4">
        Regime History (Last 50 Changes)
      </p>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-gray-500 border-b border-gray-200">
              <th className="pb-3 font-medium">Time</th>
              <th className="pb-3 font-medium">Regime</th>
              <th className="pb-3 font-medium">Confidence</th>
              <th className="pb-3 font-medium">Classifier</th>
            </tr>
          </thead>
          <tbody>
            {history.slice(0, 50).map((entry, idx) => {
              const color = REGIME_COLORS[entry.regime] || '#6b7280';
              return (
                <tr key={idx} className="border-b border-gray-100 hover:bg-gray-50">
                  <td className="py-3 text-gray-600 font-mono text-xs">
                    {timeAgo(entry.timestamp)}
                  </td>
                  <td className="py-3">
                    <div className="flex items-center gap-2">
                      <div
                        className="w-2 h-2 rounded-full"
                        style={{ backgroundColor: color }}
                      />
                      <span className="font-medium text-gray-900">
                        {REGIME_LABELS[entry.regime] || entry.regime}
                      </span>
                    </div>
                  </td>
                  <td className="py-3 text-gray-700 font-mono">
                    {(entry.confidence * 100).toFixed(1)}%
                  </td>
                  <td className="py-3 text-gray-600 text-xs">
                    {entry.classifier || 'Unknown'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default function MarketRegime() {
  const [refreshing, setRefreshing] = useState(false);

  const {
    data: regime,
    isLoading: regimeLoading,
    isError: regimeError,
    refetch: refetchRegime,
  } = useQuery({
    queryKey: ['current-regime'],
    queryFn: getCurrentRegime,
    refetchInterval: 60000, // 60 seconds
    staleTime: 30000, // 30 seconds
  });

  const {
    data: historyData,
    isLoading: historyLoading,
    isError: historyError,
    refetch: refetchHistory,
  } = useQuery({
    queryKey: ['regime-history'],
    queryFn: getRegimeHistory,
    refetchInterval: 120000, // 120 seconds
    staleTime: 60000, // 60 seconds
  });

  const history = historyData?.history || [];

  const handleRefresh = async () => {
    setRefreshing(true);
    try {
      await Promise.all([refetchRegime(), refetchHistory()]);
    } finally {
      setRefreshing(false);
    }
  };

  return (
    <div className="max-w-7xl mx-auto px-4 py-6 space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold text-gray-900">Market Regime</h1>
          <p className="text-sm text-gray-500 mt-1">Real-time regime classification and analysis</p>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {refreshing ? 'Refreshing...' : 'Refresh'}
        </button>
      </div>

      {/* Current Regime Card (Full Width) */}
      <CurrentRegimeCard
        regime={regime}
        isLoading={regimeLoading}
        isError={regimeError}
      />

      {/* Two-Column Layout */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* HMM Probability Distribution */}
        <HMMProbabilityDistribution
          regime={regime}
          isLoading={regimeLoading}
          isError={regimeError}
        />

        {/* Regime Statistics */}
        <RegimeStatistics
          history={history}
          isLoading={historyLoading}
          isError={historyError}
        />
      </div>

      {/* Regime History Table (Full Width) */}
      <RegimeHistoryTable
        history={history}
        isLoading={historyLoading}
        isError={historyError}
      />
    </div>
  );
}
