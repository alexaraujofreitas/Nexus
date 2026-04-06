import { useState, useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Crosshair, BookOpen, ChevronDown, CheckCircle2, XCircle, Clock } from 'lucide-react';
import { getPipelineStatus } from '../api/scanner';
import { cn } from '../lib/utils';

// ── Strategy status types ────────────────────────────────
type StrategyStatus = 'active' | 'disabled' | 'research';

interface StrategyMeta {
  name: string;
  key: string;
  status: StrategyStatus;
  category: string;
  regime: string;
  direction: string;
  backtest_pf: string;
  backtest_wr: string;
  description: string;
}

const STRATEGIES: StrategyMeta[] = [
  {
    name: 'Pullback Long',
    key: 'pullback_long',
    status: 'active',
    category: 'Trend Continuation',
    regime: 'Bull Trend',
    direction: 'Long Only',
    backtest_pf: '0.90 (solo) / 1.27 (combined)',
    backtest_wr: '44.6%',
    description: 'Enters long positions on pullbacks to the 50 EMA during confirmed bull trends, validated by rejection candle structure and 4h higher-timeframe confirmation.',
  },
  {
    name: 'Swing Low Continuation',
    key: 'swing_low_continuation',
    status: 'active',
    category: 'Trend Continuation',
    regime: 'Bear Trend',
    direction: 'Short Only',
    backtest_pf: '1.55',
    backtest_wr: '60.9%',
    description: 'Enters short positions when price makes new swing lows during confirmed bear trends, confirmed by strong ADX readings on the 1h timeframe.',
  },
  {
    name: 'Funding Rate',
    key: 'funding_rate',
    status: 'active',
    category: 'Market Microstructure',
    regime: 'All Regimes',
    direction: 'Long & Short',
    backtest_pf: '\u2014',
    backtest_wr: '\u2014',
    description: 'Contrarian strategy that fades extreme perpetual funding rates, entering against the crowded side when funding diverges significantly from equilibrium.',
  },
  {
    name: 'Sentiment',
    key: 'sentiment',
    status: 'active',
    category: 'NLP / Sentiment',
    regime: 'All (excl. Crisis)',
    direction: 'Long & Short',
    backtest_pf: '\u2014',
    backtest_wr: '\u2014',
    description: 'GPU-accelerated FinBERT and VADER NLP analysis of crypto news and social media feeds, generating directional signals from aggregate sentiment scores.',
  },
  {
    name: 'Trend',
    key: 'trend',
    status: 'disabled',
    category: 'Trend Following',
    regime: 'Bull / Bear Trend',
    direction: 'Long & Short',
    backtest_pf: '0.96',
    backtest_wr: '50.3%',
    description: 'EMA crossover and ADX-based trend following strategy. Disabled in Session 48 \u2014 net-negative at 0.04%/side commission structure.',
  },
  {
    name: 'Momentum Breakout',
    key: 'momentum_breakout',
    status: 'disabled',
    category: 'Breakout',
    regime: 'Volatility Expansion',
    direction: 'Long & Short',
    backtest_pf: '4.17 (zero fee) / 1.24 (with fee)',
    backtest_wr: '63.5%',
    description: 'Enters on range breakouts with volume confirmation. High PF at zero fee, but thin edge eroded by fees. Disabled after Session 49 optimisation study.',
  },
  {
    name: 'Mean Reversion',
    key: 'mean_reversion',
    status: 'disabled',
    category: 'Mean Reversion',
    regime: 'Ranging',
    direction: 'Long & Short',
    backtest_pf: '0.21',
    backtest_wr: '32.2%',
    description: 'Bollinger Band and RSI-based mean reversion. Archived in v1.2 after 13-month backtest showed consistent negative expectancy (\u2212$18k).',
  },
  {
    name: 'Liquidity Sweep',
    key: 'liquidity_sweep',
    status: 'disabled',
    category: 'Market Microstructure',
    regime: 'All Regimes',
    direction: 'Long & Short',
    backtest_pf: '0.28',
    backtest_wr: '19.3%',
    description: 'Detects and fades stop-hunt sweeps beyond swing highs/lows. Archived in v1.2 after 13-month backtest showed consistent negative expectancy (\u2212$15k).',
  },
  {
    name: 'Donchian Breakout',
    key: 'donchian_breakout',
    status: 'research',
    category: 'Breakout',
    regime: 'All Regimes (weighted)',
    direction: 'Long & Short',
    backtest_pf: '1.11 (zero fee)',
    backtest_wr: '\u2014',
    description: 'Donchian channel breakout with volume and RSI confirmation. Research candidate from Session 48 \u2014 gated in disabled_models pending parameter tuning.',
  },
];

// ── Strategy Card ────────────────────────────────────────
function StrategyCard({ strategy, modelsFired }: { strategy: StrategyMeta; modelsFired: Set<string> }) {
  const isFiring = modelsFired.has(strategy.key);
  return (
    <div className={cn(
      'bg-white rounded-xl border p-4 transition-colors',
      strategy.status === 'active' ? 'border-gray-200' : 'border-gray-100 opacity-70',
    )}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-sm text-gray-900">{strategy.name}</span>
        <div className="flex items-center gap-2">
          {strategy.status === 'active' && isFiring && (
            <span className="flex items-center gap-1 px-1.5 py-0.5 rounded bg-blue-50 text-blue-600 text-[10px] font-bold uppercase">
              <span className="w-1.5 h-1.5 rounded-full bg-blue-500 animate-pulse" /> Firing
            </span>
          )}
          {strategy.status === 'active' ? (
            <span className="flex items-center gap-1 text-xs text-green-600"><CheckCircle2 className="w-3.5 h-3.5" /> Active</span>
          ) : strategy.status === 'research' ? (
            <span className="flex items-center gap-1 text-xs text-amber-600"><Clock className="w-3.5 h-3.5" /> Research</span>
          ) : (
            <span className="flex items-center gap-1 text-xs text-gray-400"><XCircle className="w-3.5 h-3.5" /> Disabled</span>
          )}
        </div>
      </div>
      <div className="flex flex-wrap gap-1.5 mb-2">
        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-blue-50 text-blue-600">
          {strategy.category}
        </span>
        <span className="px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider bg-gray-100 text-gray-500">
          {strategy.regime}
        </span>
        <span className={cn(
          'px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider',
          strategy.direction === 'Long Only' ? 'bg-green-50 text-green-600' :
          strategy.direction === 'Short Only' ? 'bg-red-50 text-red-600' :
          'bg-purple-50 text-purple-600',
        )}>
          {strategy.direction}
        </span>
      </div>
      <p className="text-xs text-gray-500 leading-relaxed mb-3">{strategy.description}</p>
      <div className="flex gap-4 text-[11px] text-gray-400">
        <span>PF: <span className="font-mono text-gray-600">{strategy.backtest_pf}</span></span>
        <span>WR: <span className="font-mono text-gray-600">{strategy.backtest_wr}</span></span>
      </div>
    </div>
  );
}

// ── Strategy Reference Guide data ────────────────────────
interface GuideEntry {
  id: string;
  title: string;
  category: string;
  badge_bg: string;
  badge_text: string;
  status: StrategyStatus;
  definition: string;
  entry_conditions: string[];
  exit_mechanics: string;
  exit_details: string[];
  key_parameters: string[];
  caution: string;
}

const GUIDE_ENTRIES: GuideEntry[] = [
  {
    id: 'mtf_confirmation',
    title: 'Multi-Timeframe Confirmation',
    category: 'System-Wide Gate',
    badge_bg: 'bg-indigo-50',
    badge_text: 'text-indigo-600',
    status: 'active',
    definition:
      'Multi-Timeframe (MTF) Confirmation is a system-wide risk gate that validates every trade candidate against the regime classification on a higher timeframe before allowing execution. Its purpose is to prevent entries that conflict with the dominant structural trend, reducing the frequency of trades taken against the prevailing macro direction.',
    entry_conditions: [
      'After the ConfluenceScorer produces an OrderCandidate, the Risk Gate checks the higher-timeframe regime for directional conflict',
      'Long signals are rejected if the higher-timeframe regime contains "bear" (e.g., bear_trend)',
      'Short signals are rejected if the higher-timeframe regime contains "bull" (e.g., bull_trend)',
      'Non-directional higher-TF regimes (ranging, volatility_expansion, accumulation, etc.) do not trigger rejection \u2014 the signal passes through',
      'If no higher-timeframe data is available (e.g., during startup or data gaps), the gate is bypassed and the signal proceeds',
    ],
    exit_mechanics:
      'MTF Confirmation does not affect exit mechanics. It is purely an entry-side filter that prevents new positions from being opened against the higher-timeframe structural direction.',
    exit_details: [
      'Timeframe mapping: 5m confirms against 15m, 15m against 1h, 30m against 4h, 1h against 4h, 4h against 1d',
      'The higher-timeframe regime is classified using the same HMM + Rule-Based ensemble as the primary timeframe',
      'Configurable via multi_tf.confirmation_required (default: true) \u2014 can be disabled entirely',
      'In backtesting mode, MTF confirmation is skipped (higher_tf_regime remains empty)',
    ],
    key_parameters: [
      'Gate toggle: multi_tf.confirmation_required (default: true)',
      'Timeframe map: configurable per primary TF in multi_tf.confirmation_timeframes',
      'Rejection reason: "MTF conflict: {side} signal vs higher-TF regime \'{regime}\'"',
      'Combined with the primary Phase 5 configuration, the 30m \u2192 4h gate is the active production path',
    ],
    caution:
      'MTF Confirmation prevents counter-trend entries but can also filter out legitimate reversal signals at structural turning points. When a bear trend is transitioning to a bull trend on the higher timeframe, early long signals on the lower timeframe will be rejected until the higher-TF regime formally transitions. This lag is by design \u2014 the system favours confirmation over early entry.',
  },
  {
    id: 'model_toggles',
    title: 'Model Toggles & Selection Pipeline',
    category: 'System-Wide Gate',
    badge_bg: 'bg-indigo-50',
    badge_text: 'text-indigo-600',
    status: 'active',
    definition:
      'The Model Toggle system controls which signal models participate in each scan cycle through a multi-layered filtering pipeline. Models pass through five sequential gates before they are allowed to evaluate: config disable gate, auto-performance gate, hard regime gate, adaptive activation gate, and feature gate. This architecture allows precise operational control while maintaining safety boundaries.',
    entry_conditions: [
      'Config Disable Gate: models listed in disabled_models config are skipped entirely without evaluation \u2014 the primary operator control for enabling/disabling strategies',
      'Auto-Performance Gate: the ModelPerformanceTracker can automatically disable underperforming models based on rolling win rate, profit factor, and expectancy thresholds',
      'Hard Regime Gate (ACTIVE_REGIMES): each model declares which regimes it can fire in. If the current regime is not in the list, the model is skipped regardless of all other factors',
      'Adaptive Activation Gate: when enabled, uses the model\'s REGIME_AFFINITY weights and the regime probability distribution to compute a continuous activation weight. Models below the minimum threshold (default 0.10) are skipped',
      'Feature Gate (PBL/SLC): Pullback Long and Swing Low Continuation are additionally gated behind mr_pbl_slc.enabled, requiring explicit opt-in',
    ],
    exit_mechanics:
      'Model Toggles affect only which models participate in signal generation. They do not influence exit mechanics for existing positions.',
    exit_details: [
      'Only models that pass ALL five gates call their evaluate() method',
      'Disabled models retain their code and weight definitions \u2014 re-enabling requires only a config change, not a code deployment',
      'The auto-disable requires ALL of: WR < 40%, expectancy < \u22120.10R, PF < 0.85, no positive-expectancy regimes, AND \u226550 trades. This conservative threshold prevents premature disabling from small samples',
      'Regime affinity weights determine probabilistic activation strength: 1.0 = full activation, 0.0 = hard block (used for crisis and liquidation_cascade regimes)',
    ],
    key_parameters: [
      'disabled_models: [] \u2014 list of model names to permanently disable',
      'adaptive_activation.enabled: true \u2014 enable probabilistic regime-based activation',
      'adaptive_activation.min_activation_weight: 0.10 \u2014 minimum activation threshold',
      'mr_pbl_slc.enabled: true \u2014 enable PBL/SLC research models',
      'Currently disabled: trend, momentum_breakout, mean_reversion, liquidity_sweep, donchian_breakout',
    ],
    caution:
      'Disabling a model removes it from ALL future scan cycles immediately. Any pending signals from that model that have not yet been executed are unaffected, but no new signals will be generated. Re-enabling a previously disabled model does not retroactively generate missed signals. Always verify the model\'s backtest performance before re-enabling.',
  },
  {
    id: 'pullback_long',
    title: 'Pullback Long (PBL)',
    category: 'Trend Continuation',
    badge_bg: 'bg-green-50',
    badge_text: 'text-green-600',
    status: 'active',
    definition:
      'Pullback Long is a trend-continuation strategy that enters long positions when price temporarily retraces to the 50-period Exponential Moving Average during a confirmed bull trend. The strategy requires a specific rejection candle structure (bullish body with dominant lower wick) as evidence that buyers are defending the moving average, combined with RSI confirmation and a 4-hour higher-timeframe structural gate.',
    entry_conditions: [
      'Regime must be bull_trend (hard-gated via ACTIVE_REGIMES)',
      'EMA Proximity: price must be within 0.4\u00d7 ATR of the 50-period EMA (configurable), confirming the pullback has reached the key support level',
      'Rejection Candle: the signal bar must be a bullish candle (close > open) with a lower wick greater than the body multiplied by the wick strength parameter (default 1.5\u00d7), and the lower wick must exceed the upper wick',
      'RSI Gate: RSI(14) must be above 45 (configurable), confirming the pullback has not entered oversold territory that would suggest trend failure',
      '4h HTF Gate: on the 4-hour timeframe, EMA(20) must be above EMA(50), confirming the higher-timeframe trend structure is intact',
    ],
    exit_mechanics:
      'Exits are ATR-based with a partial close mechanism. The initial stop is placed below entry at a fixed ATR multiple, and the target is placed above entry at a larger ATR multiple. At 1R profit (when unrealised gain equals the initial risk), 33% of the position is closed and the stop loss is moved to breakeven.',
    exit_details: [
      'Stop Loss: entry price \u2212 3.0\u00d7 ATR(14) (configurable via mr_pbl_slc.pullback_long.sl_atr_mult)',
      'Take Profit: entry price + 4.0\u00d7 ATR(14) (configurable via mr_pbl_slc.pullback_long.tp_atr_mult)',
      'Partial Close: 33% of position at 1R, then stop \u2192 breakeven (system-wide v1.2 exit mode)',
      'Entry Price: signal bar close (no buffer)',
    ],
    key_parameters: [
      'EMA proximity: 0.4\u00d7 ATR (ema_prox_atr_mult)',
      'SL/TP multipliers: 3.0\u00d7 / 4.0\u00d7 ATR (optimised in Session 50)',
      'RSI minimum: 45 (rsi_min)',
      'Wick strength: 1.5\u00d7 body (wick_strength, added Session 50)',
      'Backtest (4-year combined with SLC): PF 1.27 (with 0.04%/side fees), CAGR 47.4%',
    ],
    caution:
      'PBL has a standalone PF of 0.90 (below breakeven in isolation). It is profitable only when combined with SLC in the portfolio, where diversification and partial-close mechanics produce a combined PF of 1.27+. Do not evaluate PBL performance in isolation \u2014 the combined system is the validated configuration.',
  },
  {
    id: 'swing_low_continuation',
    title: 'Swing Low Continuation (SLC)',
    category: 'Trend Continuation',
    badge_bg: 'bg-red-50',
    badge_text: 'text-red-600',
    status: 'active',
    definition:
      'Swing Low Continuation is a trend-continuation strategy that enters short positions when price makes a new swing low during a confirmed bear trend. The strategy uses ADX on the 1-hour timeframe as a trend-strength gate, requiring strong directional momentum before committing to a continuation short. SLC is the higher-quality partner in the PBL+SLC system, with a standalone PF of 1.55.',
    entry_conditions: [
      'Regime must be bear_trend (hard-gated via ACTIVE_REGIMES)',
      'ADX Gate: 1-hour ADX(14) must be at or above 28 (configurable), confirming strong directional momentum on the intermediate timeframe',
      'Swing Low Gate: the current close must be below the minimum of all closes over the prior 10 bars (configurable), establishing a new swing low in the sequence',
      'All three conditions must be met simultaneously on the same bar',
    ],
    exit_mechanics:
      'Exits use ATR-based stop and target levels. The stop is placed above entry to protect against trend reversal, and the target is placed below entry to capture continued downside momentum. The v1.2 partial-close mechanism applies.',
    exit_details: [
      'Stop Loss: entry price + 2.5\u00d7 ATR(14) (above entry, configurable via mr_pbl_slc.swing_low_continuation.sl_atr_mult)',
      'Take Profit: entry price \u2212 2.0\u00d7 ATR(14) (below entry, configurable via mr_pbl_slc.swing_low_continuation.tp_atr_mult)',
      'Partial Close: 33% of position at 1R, then stop \u2192 breakeven',
      'Entry Price: signal bar close',
    ],
    key_parameters: [
      'ADX minimum: 28 (adx_min)',
      'Swing lookback: 10 bars (swing_bars)',
      'SL/TP multipliers: 2.5\u00d7 / 2.0\u00d7 ATR',
      'Backtest standalone: PF 1.55, WR 60.9%, n=1,229 trades (4-year, zero-fee)',
      'The tighter TP (2.0\u00d7 vs PBL\'s 4.0\u00d7) reflects the shorter duration of bear-trend continuation moves',
    ],
    caution:
      'SLC fires exclusively in bear_trend regimes. During sideways or uncertain markets, no SLC signals are generated. If the regime classifier misidentifies a bear trend (e.g., a temporary dip in a bull market), SLC may enter shorts that are quickly reversed. The ADX gate (28 minimum) mitigates this by requiring strong trend conviction before entry.',
  },
  {
    id: 'funding_rate',
    title: 'Funding Rate',
    category: 'Market Microstructure',
    badge_bg: 'bg-cyan-50',
    badge_text: 'text-cyan-600',
    status: 'active',
    definition:
      'The Funding Rate strategy generates contrarian signals based on extreme perpetual futures funding rates observed across exchanges. When funding rates diverge significantly from equilibrium (indicating crowded positioning), the strategy signals a position in the opposite direction, capitalising on the statistical tendency of crowded trades to mean-revert.',
    entry_conditions: [
      'The absolute funding rate signal magnitude from the FundingRateAgent must exceed 0.40 (configurable), indicating a significant deviation from neutral funding',
      'Agent confidence must be at or above 0.55 (configurable), ensuring the signal is based on sufficient and recent data',
      'Data must not be stale \u2014 the agent enforces a 5-minute TTL on its cache',
      'Signal direction is CONTRARIAN: extreme positive funding (longs crowded) generates a long signal; extreme negative funding (shorts crowded) generates a short signal',
      'This model fires in all regimes \u2014 it has no ACTIVE_REGIMES restriction',
    ],
    exit_mechanics:
      'Exits use standard ATR-based levels. Since funding rate signals are confirming/contextual rather than primary, the strategy uses moderate ATR multiples for both stop and target.',
    exit_details: [
      'Stop Loss: 1.5\u00d7 ATR(14) against entry direction',
      'Take Profit: 2.5\u00d7 ATR(14) in signal direction',
      'Entry Price: current close (no buffer)',
      'Signal strength: |funding_signal| \u00d7 confidence (typically 0.30\u20130.70)',
    ],
    key_parameters: [
      'Minimum signal magnitude: 0.40 (models.funding_rate.min_signal)',
      'Minimum confidence: 0.55 (models.funding_rate.min_confidence)',
      'Data source: FundingRateAgent singleton (Bybit perpetual funding, 5-min TTL cache)',
      'Model weight in confluence: 0.20 (lower weight \u2014 contextual enrichment, not primary signal)',
      'Regime affinity: moderate across all regimes (0.40\u20130.80), zero in crisis/liquidation',
    ],
    caution:
      'Funding rate signals can persist for extended periods during strong trending markets. Fading an extreme funding rate in a strong trend is a counter-trend trade and carries inherent risk. The low model weight (0.20) in the confluence system ensures funding rate signals contribute context rather than drive decisions independently. This strategy should never be the sole basis for a trade.',
  },
  {
    id: 'sentiment',
    title: 'Sentiment',
    category: 'NLP / Sentiment',
    badge_bg: 'bg-pink-50',
    badge_text: 'text-pink-600',
    status: 'active',
    definition:
      'The Sentiment strategy uses GPU-accelerated FinBERT (a BERT-based financial sentiment model) and VADER to analyse crypto news headlines and social media content in real-time. Headlines are fetched from CryptoPanic API and 4 RSS feeds, scored for sentiment polarity, and aggregated into a per-symbol directional signal. The strategy generates long signals on strongly positive aggregate sentiment and short signals on strongly negative sentiment.',
    entry_conditions: [
      'The absolute net sentiment score must exceed 0.35 (configurable), indicating a strong directional sentiment bias',
      'A minimum of 3 headlines (configurable) must be available for the symbol to ensure statistical significance',
      'Agent confidence must be at or above 0.55 (configurable)',
      'Headlines must be no older than 480 minutes (8 hours, configurable) to ensure relevance',
      'Positive net score generates a LONG signal; negative net score generates a SHORT signal',
      'Model fires in all regimes except crisis and liquidation_cascade',
    ],
    exit_mechanics:
      'Exits use standard ATR-based levels. Sentiment signals are confirming and carry the lowest model weight in the confluence system, reflecting their role as contextual enrichment rather than standalone trade generators.',
    exit_details: [
      'Stop Loss: 1.5\u00d7 ATR(14) against entry direction',
      'Take Profit: 2.5\u00d7 ATR(14) in signal direction',
      'Entry Price: current close (no buffer)',
      'Signal strength: min(|net_score| \u00d7 1.2, 0.95) \u2014 capped to prevent sentiment from dominating',
    ],
    key_parameters: [
      'Minimum signal: 0.35 (models.sentiment.min_signal)',
      'Minimum headlines: 3 (models.sentiment.min_headlines)',
      'Maximum headline age: 480 minutes (models.sentiment.max_age_minutes)',
      'Model weight in confluence: 0.12 (lowest weight \u2014 confirming signal only)',
      'NLP backend: FinBERT on CUDA GPU (~5\u201310ms/batch), VADER fallback on CPU',
      'Data sources: CryptoPanic API v2 + 4 RSS feeds (shared 5-min cache)',
    ],
    caution:
      'Sentiment analysis is inherently lagging \u2014 news headlines reflect events that have often already been priced in by the time they are published. The 0.12 model weight reflects this limitation. Additionally, crypto-specific jargon and sarcasm can confuse NLP models. The minimum headline count (3) and confidence threshold (0.55) serve as quality gates, but false positives remain possible during periods of ambiguous or mixed media coverage.',
  },
  {
    id: 'trend',
    title: 'Trend',
    category: 'Trend Following',
    badge_bg: 'bg-gray-50',
    badge_text: 'text-gray-500',
    status: 'disabled',
    definition:
      'The Trend strategy is a classic EMA crossover and ADX-based trend following system that enters in the direction of the prevailing trend when multiple moving average alignment conditions and momentum confirmations are met. It was the highest-weighted model (0.35) in the original system but was disabled in Session 48 after analysis showed it was net-negative at the production commission structure of 0.04%/side.',
    entry_conditions: [
      'LONG (bull_trend): EMA(9) > EMA(21), ADX(14) \u2265 25, RSI(14) between 45\u201370. Bonus signals from EMA(20) > EMA(100) and MACD > Signal Line',
      'SHORT (bear_trend): EMA(9) < EMA(21), ADX(14) \u2265 25, RSI(14) between 30\u201355. Bonus signals from EMA(20) < EMA(100) and MACD < Signal Line',
      'ACTIVE_REGIMES: bull_trend, bear_trend only',
      'Signal strength is composite: base 0.15 + EMA bonus (0.25) + MACD bonus (0.20) + ADX bonus (up to 0.40)',
    ],
    exit_mechanics:
      'ATR-based exits with regime-sensitive multipliers. Entry includes a small directional buffer to reduce whipsaw on marginal breakouts.',
    exit_details: [
      'Stop Loss: 1.5\u00d7 ATR(14) against entry',
      'Take Profit: entry + (ATR multiplier + 1.0) \u00d7 ATR(14) in signal direction',
      'Entry Buffer: 0.20\u00d7 ATR(14) beyond close in signal direction',
      'Partial close (v1.2): 33% at 1R, stop \u2192 breakeven',
    ],
    key_parameters: [
      'ADX minimum: 25 (models.trend.adx_min)',
      'RSI long range: 45\u201370, RSI short range: 30\u201355',
      'Model weight: 0.35 (highest in system when active)',
      'Backtest: PF 0.9592, WR 50.3% (net-negative at 0.04%/side fees)',
      'Disabled reason: Session 48 analysis confirmed negative expectancy at production fees',
    ],
    caution:
      'The Trend model was disabled because its edge (PF 0.96) does not survive transaction costs. It may become viable if commission rates decrease or if the ADX/RSI parameters are optimised to improve selectivity. Do not re-enable without a fresh backtest confirming PF \u2265 1.18 at the current fee structure.',
  },
  {
    id: 'momentum_breakout',
    title: 'Momentum Breakout',
    category: 'Breakout',
    badge_bg: 'bg-gray-50',
    badge_text: 'text-gray-500',
    status: 'disabled',
    definition:
      'Momentum Breakout enters when price breaks above a recent N-bar high (long) or below a recent N-bar low (short) with confirmed volume expansion. It is designed for volatility expansion regimes where compressed ranges resolve into directional moves. Despite a strong zero-fee PF of 4.17, the strategy\'s 30\u201338% win rate makes it highly sensitive to transaction costs.',
    entry_conditions: [
      'LONG: close exceeds the 20-bar high, volume \u2265 1.5\u00d7 average, RSI(14) > 55',
      'SHORT: close drops below the 20-bar low, volume \u2265 1.5\u00d7 average, RSI(14) < 45',
      'ACTIVE_REGIMES: volatility_expansion only',
      'Signal strength composed of base (0.35) + volume score (0.35) + breakout depth score (0.30)',
    ],
    exit_mechanics:
      'Uses a measured-move target: the take profit is projected by the height of the pre-breakout range. Stop is placed just inside the broken range boundary.',
    exit_details: [
      'Stop Loss (long): breakout high \u2212 1.0\u00d7 ATR(14)',
      'Take Profit (long): breakout high + range size (measured move)',
      'Stop Loss (short): breakout low + 1.0\u00d7 ATR(14)',
      'Take Profit (short): breakout low \u2212 range size',
      'Entry Buffer: 0.10\u00d7 ATR(14) beyond close',
    ],
    key_parameters: [
      'Lookback: 20 bars (models.momentum_breakout.lookback)',
      'Volume multiple: 1.5\u00d7 (models.momentum_breakout.vol_mult_min)',
      'RSI thresholds: bullish > 55, bearish < 45',
      'Backtest: PF 4.17 (zero-fee), PF 1.24 (0.04%/side), WR 63.5%',
      'Session 49 study: all combined configurations degraded portfolio PF below baseline',
    ],
    caution:
      'Momentum Breakout generates a high volume of low-win-rate trades. At 30\u201338% WR, the strategy requires large individual winners to compensate for frequent small losses. Transaction costs consume a significant portion of these winners. The strategy was extensively studied in Session 49 across 10 configurations and none improved the combined PBL+SLC portfolio. It remains disabled until OOS PF consistently exceeds 1.18.',
  },
  {
    id: 'mean_reversion',
    title: 'Mean Reversion',
    category: 'Mean Reversion',
    badge_bg: 'bg-gray-50',
    badge_text: 'text-gray-500',
    status: 'disabled',
    definition:
      'Mean Reversion enters positions when price reaches extreme Bollinger Band levels with RSI and StochRSI confirmation, expecting a reversion to the mean (BB midline). The strategy is designed exclusively for ranging regimes. It was archived in v1.2 after a comprehensive 13-month backtest demonstrated consistent negative expectancy.',
    entry_conditions: [
      'LONG: price within 15% of the lower Bollinger Band range, RSI(14) < 35 (oversold), optional bonus if StochRSI %K < 25',
      'SHORT: price within 15% of the upper Bollinger Band range, RSI(14) > 65 (overbought), optional bonus if StochRSI %K > 75',
      'ACTIVE_REGIMES: ranging only',
      'Entry uses a negative buffer (\u22120.15\u00d7 ATR) for limit-order positioning closer to the band extreme',
    ],
    exit_mechanics:
      'The take profit target is the Bollinger Band midline (the 20-period SMA), representing the statistical "mean" that price is expected to revert to. Stop loss uses regime-dependent ATR multipliers.',
    exit_details: [
      'Take Profit: BB midline (20-period SMA)',
      'Stop Loss: regime-dependent ATR multipliers \u2014 ranging: 1.5\u00d7, vol_compression: 1.875\u00d7, accumulation: 1.625\u00d7, squeeze: 2.25\u00d7',
      'Entry: close + (\u22120.15 \u00d7 ATR) = limit order inside the band',
      'Partial close (v1.2): 33% at 1R, stop \u2192 breakeven',
    ],
    key_parameters: [
      'BB distance threshold: 0.15 (within 15% of band edge)',
      'RSI oversold/overbought: 35 / 65',
      'StochRSI thresholds: 25 / 75 (bonus, not required)',
      'Backtest: PF 0.21, WR 32.2%, net loss \u2212$18k over 13 months',
      'Archived: v1.2 (2026-03-01) \u2014 do not re-enable without fresh validation',
    ],
    caution:
      'Mean reversion in crypto markets faces a structural challenge: crypto assets exhibit fat-tailed distributions with extended trending behaviour that frequently exceeds Bollinger Band extremes. The 32.2% win rate indicates that the mean is not reached before the stop is hit in approximately 2 out of 3 trades. This strategy may only become viable with tighter stop management or in specific low-volatility market regimes.',
  },
  {
    id: 'liquidity_sweep',
    title: 'Liquidity Sweep',
    category: 'Market Microstructure',
    badge_bg: 'bg-gray-50',
    badge_text: 'text-gray-500',
    status: 'disabled',
    definition:
      'Liquidity Sweep detects stop-hunt events where price briefly pierces beyond a swing high or swing low to trigger clustered stop-loss orders, then reverses. The strategy enters in the reversal direction, capitalising on the absorption of stop-hunt liquidity. It integrates with the LiquidationIntelligenceAgent to avoid entering during genuine liquidation cascades.',
    entry_conditions: [
      'LONG (bullish sweep): current bar\'s low penetrates below the 15-bar swing low, but the close recovers above it \u2014 confirming the sweep was rejected',
      'SHORT (bearish sweep): current bar\'s high penetrates above the 15-bar swing high, but the close pulls back below it',
      'Volume must spike above 1.3\u00d7 the average (confirming participation in the sweep)',
      'Sweep depth must be at least 0.10% of price (filters noise)',
      'LiquidationIntelligenceAgent cascade risk must be \u2264 0.70 (avoids entering during genuine cascades)',
      'Fires in all regimes (regime-agnostic overlay)',
    ],
    exit_mechanics:
      'Targets the opposite end of the pre-sweep range, reflecting the expectation that price will revert to its pre-sweep structure. Stop is placed just beyond the sweep extreme.',
    exit_details: [
      'Stop Loss (long): sweep low \u2212 0.5\u00d7 ATR(14) (just below the sweep extreme)',
      'Take Profit (long): pre-sweep swing high (full range recovery)',
      'Stop Loss (short): sweep high + 0.5\u00d7 ATR(14)',
      'Take Profit (short): pre-sweep swing low',
      'Entry Price: current close (immediate entry on reversal confirmation)',
    ],
    key_parameters: [
      'Swing lookback: 15 bars',
      'Minimum sweep depth: 0.10% of price',
      'Volume multiple: 1.3\u00d7 average',
      'Cascade risk cutoff: 0.70 (LiquidationIntelligenceAgent)',
      'Backtest: PF 0.28, WR 19.3%, net loss \u2212$15k over 13 months',
      'Archived: v1.2 (2026-03-01)',
    ],
    caution:
      'The 19.3% win rate is the lowest of any model in the system. The strategy\'s core thesis \u2014 that sweep events reliably reverse \u2014 does not hold in crypto markets where structural breakdowns are more common than stop hunts. The tight stop placement (0.5\u00d7 ATR beyond sweep extreme) compounds the problem by triggering on normal volatility noise. Do not re-enable without fundamental redesign of the sweep detection logic.',
  },
  {
    id: 'donchian_breakout',
    title: 'Donchian Breakout',
    category: 'Breakout',
    badge_bg: 'bg-amber-50',
    badge_text: 'text-amber-600',
    status: 'research',
    definition:
      'Donchian Breakout is a channel-based breakout strategy that enters when price closes beyond the N-period Donchian channel boundary with volume and RSI confirmation. Unlike Momentum Breakout (which only fires in volatility_expansion), Donchian Breakout is active across all regimes with probabilistic weighting, making it a more versatile breakout detection system. It was introduced in Session 48 as a research candidate to replace the Trend model.',
    entry_conditions: [
      'LONG: close exceeds the N-period Donchian channel high (excluding the current bar), volume \u2265 1.3\u00d7 average, RSI(14) \u2265 50',
      'SHORT: close drops below the N-period Donchian channel low, volume \u2265 1.3\u00d7 average, RSI(14) \u2264 50',
      'ACTIVE_REGIMES: all regimes (empty list), weighted by REGIME_AFFINITY \u2014 highest in vol_expansion (0.9), bull/bear trend (0.85), zero in crisis',
      'Signal strength: base 0.35 + volume score (0.35) + breakout depth score (0.30)',
    ],
    exit_mechanics:
      'Uses fixed ATR-based exits rather than measured-move targets, providing more consistent risk parameters across different channel widths.',
    exit_details: [
      'Stop Loss: channel boundary \u2212 1.5\u00d7 ATR(14) (inside the broken channel)',
      'Take Profit: entry + 3.0\u00d7 ATR(14) in signal direction',
      'Entry Buffer: 0.10\u00d7 ATR(14) beyond close',
      'Partial close (v1.2): 33% at 1R, stop \u2192 breakeven',
    ],
    key_parameters: [
      'Lookback: 20 bars (models.donchian_breakout.lookback)',
      'Volume multiple: 1.3\u00d7 (models.donchian_breakout.vol_mult_min)',
      'SL/TP multipliers: 1.5\u00d7 / 3.0\u00d7 ATR',
      'RSI thresholds: long \u2265 50, short \u2264 50',
      'Backtest: PF 1.11 (zero-fee) \u2014 below the 1.18 minimum for production deployment',
      'Status: gated in disabled_models, pending parameter tuning (target: PF \u2265 1.18 with fees)',
    ],
    caution:
      'Donchian Breakout is a research candidate that has not been validated for production use. Its zero-fee PF of 1.11 suggests marginal profitability that is unlikely to survive transaction costs at the current commission structure. The model is included in the codebase for ongoing research and parameter optimisation but is explicitly gated via disabled_models. Do not enable without completing the parameter tuning study outlined in the Session 48 notes.',
  },
];

const CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  'System-Wide Gate':       { bg: 'bg-indigo-50',  text: 'text-indigo-600' },
  'Trend Continuation':     { bg: 'bg-green-50',   text: 'text-green-600' },
  'Market Microstructure':  { bg: 'bg-cyan-50',    text: 'text-cyan-600' },
  'NLP / Sentiment':        { bg: 'bg-pink-50',    text: 'text-pink-600' },
  'Trend Following':        { bg: 'bg-gray-50',    text: 'text-gray-500' },
  'Breakout':               { bg: 'bg-amber-50',   text: 'text-amber-600' },
  'Mean Reversion':         { bg: 'bg-violet-50',  text: 'text-violet-600' },
};

const STATUS_CONFIG: Record<StrategyStatus, { dot: string; label: string }> = {
  active:   { dot: 'bg-green-500',  label: 'Active' },
  disabled: { dot: 'bg-gray-400',   label: 'Disabled' },
  research: { dot: 'bg-amber-500',  label: 'Research' },
};

function StrategyReferenceGuide() {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <BookOpen className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">Strategies Reference Guide</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {GUIDE_ENTRIES.map((entry) => {
          const catColor = CATEGORY_COLORS[entry.category] || { bg: 'bg-gray-50', text: 'text-gray-500' };
          const sts = STATUS_CONFIG[entry.status];
          return (
            <div key={entry.id}>
              <button
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
                className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className={cn('w-2.5 h-2.5 rounded-full shrink-0', sts.dot)} />
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

                  {/* Two-column: Entry Conditions + Exit Mechanics */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Entry Conditions</h4>
                      <ul className="space-y-1.5">
                        {entry.entry_conditions.map((point, i) => (
                          <li key={i} className="flex gap-2 text-sm text-gray-700">
                            <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                            <span className="leading-relaxed">{point}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Exit Mechanics</h4>
                      <p className="text-sm text-gray-700 leading-relaxed mb-2">{entry.exit_mechanics}</p>
                      <ul className="space-y-1.5">
                        {entry.exit_details.map((point, i) => (
                          <li key={i} className="flex gap-2 text-sm text-gray-700">
                            <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                            <span className="leading-relaxed">{point}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  {/* Key Parameters */}
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Key Parameters & Performance</h4>
                    <ul className="space-y-1.5">
                      {entry.key_parameters.map((point, i) => (
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

// ── Page Component ───────────────────────────────────────
export default function Strategy() {
  const { data: pipelineData } = useQuery({
    queryKey: ['pipeline-status-strategy'],
    queryFn: getPipelineStatus,
    refetchInterval: 30000,
    staleTime: 15000,
  });

  // Collect all models currently firing across the pipeline
  const modelsFired = useMemo(() => {
    const set = new Set<string>();
    if (pipelineData?.pipeline) {
      for (const row of pipelineData.pipeline) {
        for (const m of (row.models_fired || [])) set.add(m);
      }
    }
    return set;
  }, [pipelineData]);

  const activeStrategies = STRATEGIES.filter(s => s.status === 'active');
  const disabledStrategies = STRATEGIES.filter(s => s.status === 'disabled');
  const researchStrategies = STRATEGIES.filter(s => s.status === 'research');

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Crosshair className="w-5 h-5 text-blue-500" />
        <div>
          <h1 className="text-xl font-bold text-gray-900">Strategy</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Signal models and trade selection logic powering the execution pipeline
          </p>
        </div>
        <div className="flex gap-2 ml-auto">
          <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-green-50 border border-green-200">
            <span className="w-2 h-2 rounded-full bg-green-500" />
            <span className="text-xs font-bold text-green-700">{activeStrategies.length} Active</span>
          </span>
          <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-gray-50 border border-gray-200">
            <span className="w-2 h-2 rounded-full bg-gray-400" />
            <span className="text-xs font-bold text-gray-500">{disabledStrategies.length} Disabled</span>
          </span>
          {researchStrategies.length > 0 && (
            <span className="flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-amber-50 border border-amber-200">
              <span className="w-2 h-2 rounded-full bg-amber-500" />
              <span className="text-xs font-bold text-amber-700">{researchStrategies.length} Research</span>
            </span>
          )}
        </div>
      </div>

      {/* How Strategies Work */}
      <div className="bg-white rounded-xl border border-gray-200 p-5">
        <h2 className="text-[11px] font-bold text-gray-900 uppercase tracking-wider mb-3">How Strategies Drive Execution</h2>
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm text-gray-600 leading-relaxed">
          <div>
            <p className="font-semibold text-gray-900 text-xs uppercase tracking-wider mb-1">Signal Generation</p>
            <p>Each scan cycle evaluates every tradable symbol through all active strategies. A strategy produces a signal only when its specific entry conditions are met in the context of the current market regime. Multiple strategies can fire simultaneously on the same symbol.</p>
          </div>
          <div>
            <p className="font-semibold text-gray-900 text-xs uppercase tracking-wider mb-1">Confluence Scoring</p>
            <p>When multiple strategies fire, their signals are aggregated into a single weighted confluence score. Each strategy carries a base weight that is modulated by regime affinity, adaptive learning, and correlation dampening. The score must exceed a dynamic threshold to generate a trade candidate.</p>
          </div>
          <div>
            <p className="font-semibold text-gray-900 text-xs uppercase tracking-wider mb-1">Risk Gate & Execution</p>
            <p>Trade candidates pass through a multi-layer Risk Gate covering expected value, risk-reward ratio, portfolio heat, drawdown limits, and multi-timeframe confirmation. Only candidates that clear every gate are approved and submitted to the Paper Executor for execution.</p>
          </div>
        </div>
      </div>

      {/* Active Strategies */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Active Strategies</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-4 gap-3">
          {activeStrategies.map(s => <StrategyCard key={s.key} strategy={s} modelsFired={modelsFired} />)}
        </div>
      </div>

      {/* Research Strategies */}
      {researchStrategies.length > 0 && (
        <div>
          <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Research Candidates</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-4 gap-3">
            {researchStrategies.map(s => <StrategyCard key={s.key} strategy={s} modelsFired={modelsFired} />)}
          </div>
        </div>
      )}

      {/* Disabled Strategies */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Disabled Strategies</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-2 xl:grid-cols-4 gap-3">
          {disabledStrategies.map(s => <StrategyCard key={s.key} strategy={s} modelsFired={modelsFired} />)}
        </div>
      </div>

      {/* Reference Guide */}
      <StrategyReferenceGuide />
    </div>
  );
}
