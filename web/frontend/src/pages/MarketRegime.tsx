import { useState, useMemo, useCallback } from 'react';
import { useQuery } from '@tanstack/react-query';
import { TrendingUp, RefreshCw, BookOpen, ChevronDown } from 'lucide-react';
import { getPipelineStatus } from '../api/scanner';
import type { RegimeSnapshot } from '../api/scanner';
import { cn } from '../lib/utils';

const MAX_DISPLAY = 20;

// ── Regime visual config ─────────────────────────────────
const REGIME_CONFIG: Record<string, { label: string; bg: string; text: string; dot: string }> = {
  bull_trend:       { label: 'Bull',       bg: 'bg-green-100',  text: 'text-green-700',  dot: 'bg-green-500' },
  bear_trend:       { label: 'Bear',       bg: 'bg-red-100',    text: 'text-red-700',    dot: 'bg-red-500' },
  ranging:          { label: 'Range',      bg: 'bg-yellow-100', text: 'text-yellow-700', dot: 'bg-yellow-500' },
  vol_expansion:    { label: 'Vol+',       bg: 'bg-purple-100', text: 'text-purple-700', dot: 'bg-purple-500' },
  vol_compression:  { label: 'Vol-',       bg: 'bg-violet-100', text: 'text-violet-700', dot: 'bg-violet-500' },
  accumulation:     { label: 'Accum',      bg: 'bg-emerald-100',text: 'text-emerald-700',dot: 'bg-emerald-500' },
  distribution:     { label: 'Dist',       bg: 'bg-orange-100', text: 'text-orange-700', dot: 'bg-orange-500' },
  uncertain:        { label: 'Uncertain',  bg: 'bg-gray-100',   text: 'text-gray-500',   dot: 'bg-gray-400' },
  volatility_expansion: { label: 'Vol+',   bg: 'bg-purple-100', text: 'text-purple-700', dot: 'bg-purple-500' },
};

function formatScanTime(iso: string): string {
  if (!iso) return '—';
  const d = new Date(iso.endsWith('Z') ? iso : iso + 'Z');
  if (isNaN(d.getTime())) return '—';
  return d.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })
    + '\n' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function RegimeCell({ regime }: { regime: string | undefined }) {
  if (!regime) return <td className="px-2 py-2.5 text-center text-gray-200 text-xs">—</td>;
  const cfg = REGIME_CONFIG[regime] || REGIME_CONFIG['uncertain'];
  return (
    <td className="px-1.5 py-2 text-center">
      <span className={cn('inline-block px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide', cfg.bg, cfg.text)}>
        {cfg.label}
      </span>
    </td>
  );
}

// ── Regime Reference Guide data ─────────────────────────
interface RegimeGuideEntry {
  id: string;
  title: string;
  category: string;
  dot: string;
  badge_bg: string;
  badge_text: string;
  definition: string;
  trigger_conditions: string[];
  market_interpretation: string;
  trading_implications: string[];
  caution: string;
}

const REGIME_GUIDE: RegimeGuideEntry[] = [
  {
    id: 'bull_trend',
    title: 'Bull Trend',
    category: 'Directional',
    dot: 'bg-green-500',
    badge_bg: 'bg-green-50',
    badge_text: 'text-green-600',
    definition:
      'A sustained upward directional move characterised by strong trend momentum and rising price structure. The market is exhibiting persistent buying pressure with price consistently advancing above key moving averages.',
    trigger_conditions: [
      'Average Directional Index (ADX) must be at or above the trend threshold of 25, confirming strong directional momentum',
      'The 20-period Exponential Moving Average (EMA) must have a positive slope over the trailing 5-bar window, confirming upward price trajectory',
      'Confidence scales from 0.50 at the ADX threshold to 1.00 when ADX reaches 45 or higher',
    ],
    market_interpretation:
      'The instrument is in a well-established uptrend. Institutional participants are likely positioned long, and price is trending above the 20 EMA with increasing directional conviction. Higher ADX readings indicate stronger trend persistence and lower probability of mean-reversion setups succeeding.',
    trading_implications: [
      'Trend-following and pullback-to-support strategies are favoured',
      'Short-side setups carry elevated risk due to prevailing directional momentum',
      'Breakout continuation entries are more likely to follow through',
      'Tighter trailing stops may be appropriate as ADX approaches extreme readings (>45), which can precede trend exhaustion',
    ],
    caution:
      'A Bull Trend classification does not imply the trend will continue indefinitely. Divergences between price and momentum oscillators (e.g., RSI) can signal late-stage trends. Always monitor for regime transitions, particularly toward Distribution or Volatility Expansion.',
  },
  {
    id: 'bear_trend',
    title: 'Bear Trend',
    category: 'Directional',
    dot: 'bg-red-500',
    badge_bg: 'bg-red-50',
    badge_text: 'text-red-600',
    definition:
      'A sustained downward directional move with strong trend momentum and declining price structure. The market is exhibiting persistent selling pressure with price consistently trading below key moving averages.',
    trigger_conditions: [
      'Average Directional Index (ADX) must be at or above the trend threshold of 25, confirming strong directional momentum',
      'The 20-period Exponential Moving Average (EMA) must have a negative slope over the trailing 5-bar window, confirming downward price trajectory',
      'Confidence scales from 0.50 at the ADX threshold to 1.00 when ADX reaches 45 or higher',
    ],
    market_interpretation:
      'The instrument is in a well-established downtrend. Selling pressure dominates, and price is trending below the 20 EMA with increasing directional conviction. Rallies into resistance are more likely to be sold into rather than breaking through.',
    trading_implications: [
      'Short-side continuation and rally-into-resistance strategies are favoured',
      'Long-side entries carry elevated risk and should require exceptionally strong confirmation',
      'Swing Low Continuation setups have the highest historical edge in this regime',
      'Protective stops on existing long positions should be tightened or moved to breakeven',
    ],
    caution:
      'Bear Trends in crypto markets can exhibit rapid capitulation phases followed by sharp reversals. An extremely high ADX (>50) in a Bear Trend may signal trend exhaustion rather than acceleration. Watch for regime transitions toward Accumulation or Volatility Expansion.',
  },
  {
    id: 'ranging',
    title: 'Ranging',
    category: 'Non-Directional',
    dot: 'bg-yellow-500',
    badge_bg: 'bg-yellow-50',
    badge_text: 'text-yellow-600',
    definition:
      'A low-conviction, directionless market environment where price oscillates within a bounded range without establishing a clear trend. Neither buyers nor sellers have established control, and directional momentum is weak or absent.',
    trigger_conditions: [
      'Primary trigger: ADX falls below 20, indicating minimal directional trend strength',
      'Secondary trigger: ADX is in the dead zone between 20 and 25 without qualifying for accumulation, distribution, or a volatility state',
      'Fallback trigger: ADX is at or above 25 but the EMA slope data is unavailable, making directional classification impossible',
      'Confidence is highest when ADX is near zero (strong range evidence) and lowest near the boundary thresholds',
    ],
    market_interpretation:
      'The market is in a consolidation phase. Price is likely oscillating between identifiable support and resistance levels. This environment often occurs after a strong trend loses momentum, during periods of low participation, or ahead of major catalysts where participants are waiting for directional clarity.',
    trading_implications: [
      'Mean-reversion and range-bound strategies (buy support, sell resistance) are the most appropriate',
      'Trend-following and breakout strategies have a significantly lower probability of success',
      'Position sizes should typically be reduced due to lower conviction and higher chop risk',
      'Watch for a transition to Volatility Compression, which often precedes a breakout from the range',
    ],
    caution:
      'Ranging environments can persist far longer than expected, eroding capital through frequent stop-outs on directional trades. The transition from Ranging to a trending state is often sudden and difficult to time. Avoid forcing directional bias in a confirmed range.',
  },
  {
    id: 'vol_expansion',
    title: 'Volatility Expansion',
    category: 'Volatility',
    dot: 'bg-purple-500',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'An environment of rapidly expanding price volatility, typically signalling a major market event, breakout, or shift in participant behaviour. Bollinger Band width is significantly above its recent historical average, indicating that price swings are amplifying.',
    trigger_conditions: [
      'Bollinger Band width ratio must be at or above 1.5x the 20-bar rolling average BB width',
      'BB width is calculated as (Upper Band \u2212 Lower Band) / Middle Band',
      'This classification takes priority over trend and ranging states due to its impact on risk management',
      'Confidence scales from 0.50 at the 1.5x threshold to 1.00 at 1.75x or higher',
    ],
    market_interpretation:
      'The market is experiencing a significant expansion in price range. This can occur during breakouts from consolidation, news-driven events, liquidation cascades, or sudden shifts in market sentiment. Expanded volatility increases both opportunity and risk proportionally.',
    trading_implications: [
      'Position sizes should be reduced to account for wider stop-loss distances required by expanded ATR',
      'Breakout strategies may find high-conviction entries, but false breakouts are also more common',
      'Existing positions require wider stops to avoid being shaken out by normal volatility',
      'Risk-reward calculations should be reassessed — wider stops demand proportionally larger targets',
      'This regime often marks the beginning of a new directional trend',
    ],
    caution:
      'Volatility Expansion regimes carry the highest risk per unit of exposure. Slippage, spread widening, and liquidity thinning are common during these periods. Never size positions based on pre-expansion volatility metrics. If this regime coincides with extreme RSI readings (<28), a Crisis or Liquidation Cascade may be developing.',
  },
  {
    id: 'vol_compression',
    title: 'Volatility Compression',
    category: 'Volatility',
    dot: 'bg-violet-500',
    badge_bg: 'bg-violet-50',
    badge_text: 'text-violet-600',
    definition:
      'An environment of abnormally narrow price volatility, where Bollinger Bands are tightly contracted relative to their recent historical average. This typically precedes a significant directional move as energy builds within the compressed range.',
    trigger_conditions: [
      'Bollinger Band width ratio must be at or below 0.6x the 20-bar rolling average BB width (a 40% contraction)',
      'This classification takes priority over trend and ranging states due to its predictive value',
      'Confidence scales from 0.50 at the 0.6x threshold to 1.00 when the ratio drops to 0.2x or lower',
    ],
    market_interpretation:
      'The market is coiling. Institutional participants are often accumulating or distributing positions during compression phases while retail activity declines. Historically, extended compression periods are followed by explosive directional moves — the tighter and longer the compression, the more powerful the subsequent breakout tends to be.',
    trading_implications: [
      'Anticipatory breakout positioning (with tight risk) can be highly rewarding',
      'Directional bias should come from other factors (order flow, funding rates, macro) rather than the compression itself',
      'Avoid holding large directional positions established during compression — the breakout direction is uncertain',
      'Options or straddle-like strategies (if available) are theoretically optimal in this regime',
      'Monitor for the transition to Volatility Expansion, which confirms the breakout is underway',
    ],
    caution:
      'Compression regimes provide no directional bias. The subsequent breakout can move in either direction with equal probability. Premature directional commitment based on compression alone is one of the most common retail trading errors. Wait for the expansion signal before committing significant capital.',
  },
  {
    id: 'accumulation',
    title: 'Accumulation',
    category: 'Structural',
    dot: 'bg-emerald-500',
    badge_bg: 'bg-emerald-50',
    badge_text: 'text-emerald-600',
    definition:
      'A structural phase where informed participants are systematically building positions during a low-volatility, non-trending environment. Rising volume combined with recovering RSI and low directional momentum suggests that buying pressure is building beneath a calm surface.',
    trigger_conditions: [
      'ADX must be below 20 (no established trend)',
      'Volume trend must exceed +10% — calculated as the percentage difference between 20-bar average volume and 60-bar average volume',
      'RSI must be between 30 and 55 — indicating a recovery from oversold conditions without being overbought',
      'All three conditions must be satisfied simultaneously',
      'Confidence scales with the magnitude of the volume trend, starting at 0.50',
    ],
    market_interpretation:
      'Smart money is likely accumulating. The combination of rising volume in a non-trending market with recovering RSI readings is a hallmark of institutional position building. Price may appear "boring" on the surface while significant capital is being deployed. This phase often precedes a Bull Trend regime transition.',
    trading_implications: [
      'Early long entries with patient time horizons are favoured',
      'Stop-loss placement should account for the possibility of one final shakeout before the breakout',
      'Volume analysis becomes the primary confirmation tool in this regime',
      'A transition from Accumulation to Bull Trend is a high-conviction long signal',
      'Short positions should be closed or significantly reduced',
    ],
    caution:
      'Accumulation phases can be indistinguishable from Ranging phases in their early stages. The volume trend is the critical differentiator — without genuine volume expansion, apparent accumulation may simply be noise within a range. False accumulation signals occasionally precede distribution events.',
  },
  {
    id: 'distribution',
    title: 'Distribution',
    category: 'Structural',
    dot: 'bg-orange-500',
    badge_bg: 'bg-orange-50',
    badge_text: 'text-orange-600',
    definition:
      'A structural phase where informed participants are systematically exiting or reversing positions while price remains near recent highs. Declining volume combined with elevated RSI readings and proximity to swing highs suggests that selling pressure is building beneath apparent stability.',
    trigger_conditions: [
      'ADX must be below 20 (no established trend)',
      'Volume trend must be below \u221210% — declining volume relative to the 60-bar baseline',
      'RSI must be at or above 60 — indicating overbought or late-cycle conditions',
      'Price must be within 5% of the 20-bar high (near highs, not pulling back)',
      'All four conditions must be satisfied simultaneously',
      'Confidence scales with the magnitude of the volume decline, starting at 0.50',
    ],
    market_interpretation:
      'Smart money is likely distributing. Price holding near highs on declining volume is a classic distribution signature — sellers are unloading into buy-side liquidity while creating the appearance of price stability. This phase frequently precedes a Bear Trend regime transition or a sharp corrective move.',
    trading_implications: [
      'Long positions should be tightened with trailing stops or partially closed',
      'New long entries are not recommended — the risk/reward profile is poor near distribution highs',
      'Short setups with confirmation (e.g., a breakdown below the range) carry a favourable edge',
      'A transition from Distribution to Bear Trend is a high-conviction short signal',
      'Watch for volume spikes on down candles as confirmation of distribution completing',
    ],
    caution:
      'Distribution is one of the most difficult regimes to trade proactively because price often appears strong while the underlying structure weakens. Premature short entries during distribution can result in stop-outs against marginal new highs. Wait for structural confirmation (range breakdown or regime transition) before committing to the short side.',
  },
  {
    id: 'uncertain',
    title: 'Uncertain',
    category: 'Unclassified',
    dot: 'bg-gray-400',
    badge_bg: 'bg-gray-50',
    badge_text: 'text-gray-500',
    definition:
      'An ambiguous market state where the classification system cannot assign a regime with sufficient confidence. This occurs when key indicator data is unavailable, when the market exhibits conflicting signals across multiple dimensions, or during the initial warmup period before the hysteresis buffer stabilises.',
    trigger_conditions: [
      'ADX data is unavailable or contains invalid values (NaN), preventing trend strength assessment',
      'The regime classifier is in its initial warmup phase (first 3 bars) before the hysteresis buffer has filled',
      'No other regime conditions are met after exhausting all classification rules in priority order',
      'Default confidence is set to 0.30 — the lowest of any regime classification',
    ],
    market_interpretation:
      'The classifier lacks sufficient evidence to make a definitive assessment. This should be treated as a neutral signal — neither bullish nor bearish. In practice, Uncertain regimes most commonly appear at system startup (transient and expected) or when data quality issues prevent indicator computation.',
    trading_implications: [
      'No new positions should be initiated based on regime signal alone',
      'Existing positions should maintain their current risk parameters without adjustment',
      'If Uncertain persists beyond the warmup period, investigate potential data feed or indicator computation issues',
      'Treat as equivalent to Ranging from a risk management perspective — reduce directional exposure',
    ],
    caution:
      'An Uncertain classification that persists for more than a few scan cycles outside of system startup may indicate a data integrity issue rather than a genuinely ambiguous market. Check that OHLCV data is being received, that indicator columns are computed successfully, and that the ADX calculation has sufficient history.',
  },
];

const CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  Directional:    { bg: 'bg-blue-50',   text: 'text-blue-600' },
  'Non-Directional': { bg: 'bg-amber-50', text: 'text-amber-600' },
  Volatility:     { bg: 'bg-purple-50', text: 'text-purple-600' },
  Structural:     { bg: 'bg-teal-50',   text: 'text-teal-600' },
  Unclassified:   { bg: 'bg-gray-50',   text: 'text-gray-500' },
};

function RegimeReferenceGuide() {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <BookOpen className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">Regime Reference Guide</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {REGIME_GUIDE.map((entry) => {
          const catColor = CATEGORY_COLORS[entry.category] || CATEGORY_COLORS['Unclassified'];
          return (
            <div key={entry.id}>
              <button
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
                className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className={cn('w-2.5 h-2.5 rounded-full shrink-0', entry.dot)} />
                  <span className="text-sm font-semibold text-gray-900">{entry.title}</span>
                  <span className={cn('px-2 py-0.5 rounded text-[10px] font-bold uppercase tracking-wider', catColor.bg, catColor.text)}>
                    {entry.category}
                  </span>
                </div>
                <ChevronDown className={cn(
                  'w-4 h-4 text-gray-400 transition-transform duration-200',
                  open === entry.id && 'rotate-180',
                )} />
              </button>

              {open === entry.id && (
                <div className="px-5 pb-5 space-y-4 animate-in fade-in duration-200">
                  {/* Definition */}
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Definition</h4>
                    <p className="text-sm text-gray-700 leading-relaxed">{entry.definition}</p>
                  </div>

                  {/* Two-column: Interpretation + Trigger Conditions */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Market Interpretation</h4>
                      <p className="text-sm text-gray-700 leading-relaxed">{entry.market_interpretation}</p>
                    </div>
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Classification Criteria</h4>
                      <ul className="space-y-1.5">
                        {entry.trigger_conditions.map((point, i) => (
                          <li key={i} className="flex gap-2 text-sm text-gray-700">
                            <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                            <span className="leading-relaxed">{point}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  {/* Trading Implications */}
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Trading Implications</h4>
                    <ul className="space-y-1.5">
                      {entry.trading_implications.map((point, i) => (
                        <li key={i} className="flex gap-2 text-sm text-gray-700">
                          <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                          <span className="leading-relaxed">{point}</span>
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* Caution */}
                  <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3">
                    <p className="text-xs font-semibold text-amber-800 mb-1">Caution</p>
                    <p className="text-sm text-amber-700 leading-relaxed">{entry.caution}</p>
                  </div>
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

export default function MarketRegime() {
  const [refreshing, setRefreshing] = useState(false);

  // Poll pipeline status every 30s — regime_snapshots comes from server
  const { data: pipelineData, refetch } = useQuery({
    queryKey: ['pipeline-status-regime'],
    queryFn: getPipelineStatus,
    refetchInterval: 30000,
    staleTime: 15000,
  });

  // Use server-side regime snapshots (persisted across page navigations)
  const snapshots: RegimeSnapshot[] = useMemo(() => {
    return (pipelineData?.regime_snapshots || []).slice(0, MAX_DISPLAY);
  }, [pipelineData]);

  // Collect all unique symbols across all snapshots, sorted
  const symbols = useMemo(() => {
    const set = new Set<string>();
    for (const snap of snapshots) {
      for (const sym of Object.keys(snap.regimes)) set.add(sym);
    }
    // Also add pipeline symbols that haven't been scanned yet
    if (pipelineData?.pipeline) {
      for (const row of pipelineData.pipeline) set.add(row.symbol);
    }
    return Array.from(set).sort();
  }, [snapshots, pipelineData]);

  const handleRefresh = useCallback(async () => {
    setRefreshing(true);
    try { await refetch(); } finally { setTimeout(() => setRefreshing(false), 500); }
  }, [refetch]);

  // Summary: count current regimes (from latest snapshot)
  const currentRegimeCounts = useMemo(() => {
    if (snapshots.length === 0) return {};
    const counts: Record<string, number> = {};
    for (const [, regime] of Object.entries(snapshots[0].regimes)) {
      counts[regime] = (counts[regime] || 0) + 1;
    }
    return counts;
  }, [snapshots]);

  // Format display times for column headers
  const displayTimes = useMemo(() => {
    return snapshots.map(s => formatScanTime(s.timestamp));
  }, [snapshots]);

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <TrendingUp className="w-5 h-5 text-blue-500" />
          <div>
            <h1 className="text-xl font-bold text-gray-900">Market Regime</h1>
            <p className="text-sm text-gray-500 mt-0.5">
              Rolling regime history by tradable pair ({snapshots.length} scan{snapshots.length !== 1 ? 's' : ''} captured)
            </p>
          </div>
        </div>
        <button
          onClick={handleRefresh}
          disabled={refreshing}
          className={cn(
            'flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-semibold transition-colors',
            refreshing ? 'bg-gray-100 text-gray-400' : 'bg-blue-600 text-white hover:bg-blue-700',
          )}
        >
          <RefreshCw className={cn('w-4 h-4', refreshing && 'animate-spin')} />
          Refresh
        </button>
      </div>

      {/* Current Regime Summary */}
      {Object.keys(currentRegimeCounts).length > 0 && (
        <div className="flex flex-wrap gap-2">
          {Object.entries(currentRegimeCounts)
            .sort(([, a], [, b]) => b - a)
            .map(([regime, count]) => {
              const cfg = REGIME_CONFIG[regime] || REGIME_CONFIG['uncertain'];
              return (
                <div key={regime} className={cn('flex items-center gap-2 px-3 py-1.5 rounded-lg border', cfg.bg)}>
                  <span className={cn('w-2 h-2 rounded-full', cfg.dot)} />
                  <span className={cn('text-xs font-bold', cfg.text)}>
                    {(REGIME_CONFIG[regime]?.label || regime).toUpperCase()}
                  </span>
                  <span className={cn('text-xs font-mono', cfg.text)}>{count}</span>
                </div>
              );
            })}
        </div>
      )}

      {/* Regime Matrix Table */}
      <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
        {symbols.length === 0 || snapshots.length === 0 ? (
          <div className="p-12 text-center">
            <TrendingUp className="w-10 h-10 text-gray-200 mx-auto mb-3" />
            <p className="text-sm font-medium text-gray-500">Waiting for scan data...</p>
            <p className="text-xs text-gray-400 mt-1">Regime history will populate after the first scan cycle completes</p>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full border-collapse text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="sticky left-0 z-10 bg-gray-50 px-4 py-3 text-left text-[11px] font-bold text-gray-900 uppercase tracking-wider border-r border-gray-200 min-w-[130px]">
                    Tradable Pair
                  </th>
                  {snapshots.map((snap, i) => (
                    <th
                      key={snap.timestamp}
                      className={cn(
                        'px-2 py-2 text-center text-[10px] font-semibold uppercase tracking-wider min-w-[80px]',
                        i === 0 ? 'text-blue-700 bg-blue-50/50' : 'text-gray-500',
                      )}
                    >
                      <div className="whitespace-pre-line leading-tight">
                        {displayTimes[i]}
                      </div>
                      {i === 0 && (
                        <span className="inline-block mt-0.5 px-1 py-0 rounded text-[8px] font-bold bg-blue-100 text-blue-600">
                          LATEST
                        </span>
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {symbols.map((sym, rowIdx) => (
                  <tr key={sym} className={cn('border-b border-gray-50', rowIdx % 2 === 0 ? '' : 'bg-gray-50/30')}>
                    <td className="sticky left-0 z-10 bg-white px-4 py-2.5 font-semibold text-gray-900 text-xs border-r border-gray-100 whitespace-nowrap">
                      {sym}
                    </td>
                    {snapshots.map((snap) => (
                      <RegimeCell key={snap.timestamp} regime={snap.regimes[sym]} />
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Legend */}
      <div className="bg-white rounded-xl border border-gray-200 p-4">
        <p className="text-[11px] font-bold text-gray-900 uppercase tracking-wider mb-3">Regime Legend</p>
        <div className="flex flex-wrap gap-3">
          {Object.entries(REGIME_CONFIG)
            .filter(([key]) => !['volatility_expansion'].includes(key))
            .map(([key, cfg]) => (
              <div key={key} className="flex items-center gap-1.5">
                <span className={cn('w-2.5 h-2.5 rounded-full', cfg.dot)} />
                <span className={cn('px-1.5 py-0.5 rounded text-[10px] font-bold uppercase', cfg.bg, cfg.text)}>
                  {cfg.label}
                </span>
              </div>
            ))}
        </div>
      </div>

      {/* Regime Reference Guide */}
      <RegimeReferenceGuide />
    </div>
  );
}
