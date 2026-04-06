import { useState, useEffect } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, AlertCircle, CheckCircle, BookOpen, ChevronDown } from 'lucide-react';
import { getConfluenceSignals } from '../api/signals';
import type { ConfluenceSignal } from '../api/signals';
import { useWSStore } from '../stores/wsStore';
import { cn } from '../lib/utils';

// ── Signal Card ──────────────────────────────────────────
function SignalCard({ signal }: { signal: ConfluenceSignal }) {
  const isLong = signal.direction === 'buy' || signal.direction === 'long';
  return (
    <div className="bg-white rounded-xl border border-gray-200 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-gray-900">{signal.symbol}</span>
        <span className={cn('text-sm font-bold', isLong ? 'text-green-600' : 'text-red-600')}>
          {isLong ? '▲ LONG' : '▼ SHORT'}
        </span>
      </div>
      <div className="mb-3">
        <div className="flex items-center justify-between text-xs mb-1">
          <span className="text-gray-400">Confluence Score</span>
          <span className="font-mono text-gray-700">{signal.score?.toFixed(2)}</span>
        </div>
        <div className="w-full bg-gray-100 rounded-full h-2">
          <div className="bg-blue-500 h-2 rounded-full transition-all" style={{ width: `${Math.min(signal.score * 100, 100)}%` }} />
        </div>
      </div>
      <div className="flex flex-wrap gap-1 mb-2">
        {(signal.models || []).map((m) => (
          <span key={m} className="px-1.5 py-0.5 bg-blue-50 text-blue-600 text-xs rounded">{m}</span>
        ))}
        {signal.regime && <span className="px-1.5 py-0.5 bg-gray-100 text-gray-500 text-xs rounded">{signal.regime}</span>}
      </div>
      <div className="text-xs">
        {signal.approved ? (
          <span className="flex items-center gap-1 text-green-600"><CheckCircle className="w-3 h-3" /> Approved</span>
        ) : (
          <span className="flex items-center gap-1 text-red-500" title={signal.rejection_reason || ''}>
            <AlertCircle className="w-3 h-3" /> {signal.rejection_reason || 'No signal'}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Confluence Reference Guide data ─────────────────────
interface GuideEntry {
  id: string;
  title: string;
  category: string;
  badge_bg: string;
  badge_text: string;
  definition: string;
  details: string[];
  how_it_works: string;
  key_points: string[];
  caution: string;
}

const GUIDE_ENTRIES: GuideEntry[] = [
  {
    id: 'overview',
    title: 'What Is Confluence Scoring',
    category: 'Core Concept',
    badge_bg: 'bg-blue-50',
    badge_text: 'text-blue-600',
    definition:
      'Confluence scoring is a weighted voting system that aggregates signals from multiple independent trading models into a single normalised score between 0.0 and 1.0. Rather than relying on any single model\'s prediction, the system requires agreement across diverse analytical approaches before generating a trade candidate. A higher confluence score indicates stronger multi-model consensus on direction, entry, and risk parameters.',
    details: [
      'Each scan cycle evaluates every tradable symbol through the full signal pipeline: OHLCV data ingestion, technical indicator computation, regime classification, and parallel model evaluation',
      'Only models that produce an active signal (strength > 0) participate in the vote — dormant or disabled models are excluded and their weight is redistributed',
      'The final score incorporates static model weights, regime-specific affinity adjustments, adaptive learning multipliers, correlation dampening, and optional Market Intelligence Layer (MIL) modifiers',
      'A score that meets or exceeds the dynamic confluence threshold generates an OrderCandidate, which then passes through the Risk Gate for final approval or rejection',
    ],
    how_it_works:
      'The ConfluenceScorer receives ModelSignal objects from each active model. It first determines the dominant direction (long vs. short) via weighted voting, then computes the aggregate score using only signals aligned with the majority direction. The score is adjusted for regime affinity, adaptive weights, and correlation clustering before being compared against the dynamic threshold.',
    key_points: [
      'Score range: 0.00 (no consensus) to 1.00 (maximum consensus)',
      'Default base threshold: 0.55 — dynamically adjusted between 0.28 and 0.65 based on market conditions',
      'A score above threshold generates a candidate; below threshold produces no action',
      'The score represents model consensus strength, not a probability of profit',
    ],
    caution:
      'A high confluence score does not guarantee a profitable trade. It indicates that multiple independent models agree on direction and timing. The Risk Gate, Expected Value gate, and position sizing layers provide additional protection. Always evaluate the score in context with the current regime, the specific models that fired, and the risk-reward profile.',
  },
  {
    id: 'model_weights',
    title: 'Model Weights & Renormalisation',
    category: 'Scoring Mechanics',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'Each signal model carries a base weight reflecting its historical reliability and strategic importance within the portfolio. When fewer models fire on a given scan, the weights of active models are automatically renormalised so they sum to 1.0, ensuring the final score remains on a consistent scale regardless of how many models participate.',
    details: [
      'Pullback Long (PBL): weight 0.25 — fires exclusively in bull trend regimes with EMA pullback, rejection candle, and RSI recovery criteria',
      'Swing Low Continuation (SLC): weight 0.30 — fires exclusively in bear trend regimes with swing structure and momentum continuation criteria',
      'Funding Rate: weight 0.20 — contrarian signal derived from perpetual funding rate divergences across exchanges',
      'Sentiment: weight 0.12 — FinBERT and VADER NLP analysis of crypto news and social media feeds',
      'Orchestrator: weight 0.22 — meta-signal aggregating intelligence from all active AI agents',
      'Disabled models (Trend, MomentumBreakout, MeanReversion, LiquiditySweep, DonchianBreakout) retain their weight definitions but are excluded from scoring via the config gate',
    ],
    how_it_works:
      'At scoring time, the system collects all ModelSignal objects from models that fired (strength > 0). Each model\'s base weight is retrieved from config or the hardcoded fallback table. The weight is then multiplied by the regime affinity factor and the L1/L2 adaptive learning multiplier. Finally, all participating weights are renormalised to sum to 1.0 before computing the weighted score.',
    key_points: [
      'Only models that actively fire participate in the vote — disabled or silent models contribute zero weight',
      'Weights are configurable via model_weights.{model_name} in config.yaml',
      'Renormalisation ensures the score scale is consistent whether 2 or 8 models fire',
      'The primary model (highest effective weight) determines entry price, stop loss, and take profit levels',
    ],
    caution:
      'Entry, stop, and target prices are taken from the single highest-weighted active model — they are never averaged across models. This ensures trade parameters come from a coherent analytical framework rather than a potentially contradictory blend.',
  },
  {
    id: 'regime_affinity',
    title: 'Regime Affinity Matrix',
    category: 'Scoring Mechanics',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'The Regime Affinity matrix is a probabilistic activation table that modulates each model\'s effective weight based on the current market regime. Models that are historically effective in a given regime receive full or amplified weight, while models that underperform in that regime are attenuated or blocked entirely.',
    details: [
      'Trend-following models (Trend, MomentumBreakout): full activation (1.0) in Bull/Bear Trend, minimal (0.05–0.10) in Ranging, zero in Crisis/Liquidation Cascade',
      'Mean-reversion models: full activation (1.0) in Ranging, strong (0.80) in Volatility Compression, near-zero in trending regimes',
      'PBL/SLC research models: directional only — PBL activates in Bull Trend, SLC activates in Bear Trend, both suppressed in non-matching regimes',
      'Sentiment and Funding Rate: moderate activation across most regimes (0.40–0.80), with reduced influence during crisis events',
      'Crisis and Liquidation Cascade regimes hard-block all directional models (affinity = 0.0), effectively preventing new trade entries during extreme events',
    ],
    how_it_works:
      'When computing a model\'s effective weight, the base weight is multiplied by the affinity factor for the current regime: effective_weight = base_weight × affinity[regime]. If the regime classifier provides probability distributions across multiple regimes, the affinity is blended proportionally. An affinity of 0.0 completely removes the model from the vote for that regime.',
    key_points: [
      'Affinity values range from 0.0 (completely blocked) to 1.0 (full activation)',
      'Crisis and Liquidation Cascade regimes act as circuit breakers — all directional models are suppressed',
      'Recovery regime selectively activates trend models (0.70) while suppressing mean-reversion (0.25)',
      'Custom affinity overrides can be set via regime_affinity.{model_name} in config.yaml',
    ],
    caution:
      'The affinity matrix is a static configuration, not a learned parameter. It encodes domain knowledge about which models perform well in which regimes. If the regime classifier misidentifies the current state, the affinity adjustments can inadvertently suppress the most appropriate model. This is why regime confidence is also factored into the dynamic threshold.',
  },
  {
    id: 'adaptive_weights',
    title: 'Adaptive Learning (L1 + L2)',
    category: 'Scoring Mechanics',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'The Adaptive Weight Engine applies two layers of runtime learning adjustments to model weights based on observed trade outcomes. L1 adjusts globally by model win rate, while L2 adjusts contextually by model-regime and model-asset performance. Together, they allow the system to gradually favour models that are performing well and reduce exposure to underperformers — without requiring manual recalibration.',
    details: [
      'L1 (Global Win Rate): tracks per-model win rate over a rolling 30-trade window, applying ±15% weight adjustment. A model with 70% WR receives +15% boost; 30% WR receives -15% reduction',
      'L2 (Contextual): applies model×regime and model×asset multipliers based on granular outcome tracking. Activated after 10+ trades (full), scaled at 5–9 trades (partial), or disabled below 5 trades (fallback)',
      'Both layers are persisted to disk (data/outcome_tracker.json) and survive restarts',
      'In backtesting mode (technical_only=True), adaptive weights are disabled entirely to ensure deterministic, historically reproducible results',
    ],
    how_it_works:
      'After each model\'s base weight is multiplied by the regime affinity, it is further multiplied by the combined L1 × L2 adaptive multiplier: final_weight = base_weight × affinity × L1_multiplier × L2_multiplier. The system requires a minimum number of observed trades per model before activating each tier, preventing premature adjustment from small sample sizes.',
    key_points: [
      'L1 activation: requires ≥5 trades per model; adjustment range ±15%',
      'L2 full activation: requires ≥10 trades per model×regime or model×asset pair',
      'L2 partial activation (5–9 trades): confidence-scaled at 50% strength',
      'Adaptive weights are strictly disabled during canonical backtests to maintain reproducibility',
    ],
    caution:
      'Adaptive weights respond to recent performance, which may not predict future performance. A model that has performed well recently may be receiving elevated weight precisely at the point where its edge is exhausting. The ±15% cap on L1 prevents runaway amplification, but regime-specific L2 adjustments can compound this effect. Monitor the Diagnostics panel for effective weight breakdowns.',
  },
  {
    id: 'direction_dominance',
    title: 'Directional Dominance Check',
    category: 'Scoring Mechanics',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'The directional dominance check prevents the system from generating trade candidates when active models are split between long and short signals with insufficient conviction in either direction. If the weighted vote is too evenly divided, the confluence signal is rejected regardless of the raw score.',
    details: [
      'Dominance is calculated as: |long_weight_sum − short_weight_sum| / (long_weight_sum + short_weight_sum)',
      'The minimum required dominance is 0.30 (30%) — meaning the majority direction must carry at least 65% of the total weighted vote',
      'Signals from the minority direction are excluded from the final score calculation — only majority-aligned signals contribute',
      'This check fires before the score aggregation phase, acting as an early rejection filter',
    ],
    how_it_works:
      'All active model signals are grouped by direction (long or short) with their adaptive weights summed. The dominance ratio measures how lopsided the vote is. If dominance falls below 0.30, the system determines there is no clear consensus and rejects the candidate with the reason "Direction dominance below threshold." Otherwise, only signals aligned with the majority direction proceed to score aggregation.',
    key_points: [
      'Default threshold: 0.30 (configurable via confluence.min_direction_dominance)',
      'A dominance of 1.0 means all signals agree on direction (strongest consensus)',
      'A dominance of 0.0 means signals are perfectly split (no consensus)',
      'This prevents weak, conflicted signals from reaching the Risk Gate',
    ],
    caution:
      'The dominance check only measures directional agreement among active models. It does not assess the quality or conviction of individual signals. In regimes where only 2 models fire, a single dissenting model can cause rejection even if the agreeing model has much higher conviction. This is by design — thin consensus warrants caution.',
  },
  {
    id: 'correlation_dampening',
    title: 'Correlation Dampening',
    category: 'Scoring Mechanics',
    badge_bg: 'bg-purple-50',
    badge_text: 'text-purple-600',
    definition:
      'Correlation dampening reduces the effective weight of models that share similar analytical inputs or belong to the same logical cluster, preventing redundant signals from inflating the confluence score. If two models both rely on price-momentum indicators, their combined contribution is reduced to account for the overlap.',
    details: [
      'Models are grouped into correlation clusters based on shared indicator dependencies (e.g., Trend and MomentumBreakout both use ADX, EMA, and price structure)',
      'The dampening factor for each model in a cluster is 1/√N, where N is the number of cluster members that fired in the current cycle',
      'Example: if 3 models from the same cluster fire, each receives a dampening factor of 1/√3 ≈ 0.577 (42% weight reduction)',
      'Models in singleton clusters (no correlated peers) receive a dampening factor of 1.0 (no reduction)',
    ],
    how_it_works:
      'After computing the adaptive weight for each model, the CorrelationDampener identifies which cluster each model belongs to and counts how many cluster members are active. The dampening factor is applied as a multiplier: damped_weight = adaptive_weight × (1/√N). This ensures that adding a third correlated model to the vote increases the total contribution, but by a diminishing amount — preventing score inflation from redundant agreement.',
    key_points: [
      'Singleton models: factor = 1.0 (no dampening)',
      '2 correlated models: factor = 0.707 each (29% reduction per model)',
      '3 correlated models: factor = 0.577 each (42% reduction per model)',
      'Dampening is applied after adaptive weights but before score normalisation',
    ],
    caution:
      'Correlation clustering is statically defined based on shared indicators, not dynamically computed from signal correlation. Models may produce uncorrelated signals even if they share inputs (e.g., Trend and MomentumBreakout disagreeing on direction). The current implementation errs on the side of caution by dampening whenever cluster membership overlaps.',
  },
  {
    id: 'dynamic_threshold',
    title: 'Dynamic Confluence Threshold',
    category: 'Decision Logic',
    badge_bg: 'bg-teal-50',
    badge_text: 'text-teal-600',
    definition:
      'The dynamic confluence threshold adjusts the minimum score required to generate an OrderCandidate based on real-time market conditions. In high-confidence, low-volatility regimes, the threshold is lowered to capture more opportunities. In uncertain or volatile conditions, the threshold is raised to demand stronger consensus before acting.',
    details: [
      'Base threshold: 0.55 (configurable via idss.min_confluence_score)',
      'Regime Confidence Factor: when the regime classifier reports ≥70% confidence, the threshold is raised by 5% (stricter — confident markets need strong signals). Below 40% confidence, it is lowered by 15% (more permissive — uncertain markets accept weaker signals)',
      'Model Count Factor: when fewer models are eligible due to regime affinity or config gates, the threshold is reduced proportionally (range 0.75–1.0×)',
      'Volatility Factor: Volatility Expansion and Crisis regimes raise the threshold by 15% (demanding stronger consensus in volatile markets). Compression regimes lower it by 5%',
      'Hard floor: 0.28 — the threshold can never drop below this, even with all factors compounding downward',
      'Hard ceiling: 0.65 — the threshold can never exceed this, preventing the system from becoming too restrictive',
    ],
    how_it_works:
      'The effective threshold is computed as: base_threshold × regime_confidence_factor × model_count_factor × volatility_factor, then clamped to the [0.28, 0.65] range. This calculation runs independently for each symbol on each scan cycle. The weighted confluence score must meet or exceed this effective threshold to produce an OrderCandidate.',
    key_points: [
      'The threshold adjusts per-symbol, per-cycle — different symbols can have different effective thresholds on the same scan',
      'All three adjustment factors multiply together, allowing compound effects',
      'The floor of 0.28 ensures at least minimal model agreement is always required',
      'The ceiling of 0.65 prevents the system from becoming paralysed in adverse conditions',
    ],
    caution:
      'A lowered dynamic threshold increases trade frequency but may admit lower-quality signals. Monitor the ratio of approved-to-rejected candidates when the threshold is operating near the 0.28 floor. In highly uncertain markets (regime confidence < 40%), the threshold may drop significantly — the Risk Gate and EV gate serve as critical backstops in these conditions.',
  },
  {
    id: 'mil',
    title: 'Market Intelligence Layer (MIL)',
    category: 'Decision Logic',
    badge_bg: 'bg-teal-50',
    badge_text: 'text-teal-600',
    definition:
      'The Market Intelligence Layer enriches the pure technical confluence score with real-time market microstructure data — specifically open interest changes, liquidation flow, and the Orchestrator Engine\'s aggregated intelligence signal. MIL adjustments can increase or decrease the final score, but are hard-capped to prevent non-technical signals from dominating the decision.',
    details: [
      'Open Interest (OI) Modifier: derived from Coinglass data, measures net open interest changes across exchanges. Expanding OI in the signal direction adds a positive modifier; contracting OI adds a negative modifier',
      'Liquidation Flow Modifier: detects liquidation cascades and provides contrarian signals when extreme liquidation events create forced selling/buying pressure',
      'Orchestrator Signal: the meta-signal from the AI agent network, injected as a weighted vote (weight 0.22) when |signal| > 0.10 and confidence ≥ 0.20',
      'Hard Cap: the total MIL contribution cannot exceed 25% of the pure technical baseline score. This ensures technical analysis remains the primary decision driver',
      'Low-Baseline Guardrail: when the technical baseline is below 0.05, MIL is disabled entirely to avoid extreme ratio distortions on near-zero scores',
    ],
    how_it_works:
      'After the technical score is computed (from model weights, regime affinity, and adaptive learning), MIL modifiers are added: final_score = technical_score + oi_modifier + liquidation_modifier + orchestrator_contribution. The system then checks if the total MIL delta exceeds 25% of the technical baseline. If so, the delta is clamped symmetrically, and the capped flag is set in diagnostics.',
    key_points: [
      'MIL influence cap: 25% of the technical baseline (symmetric — applies equally to positive and negative adjustments)',
      'The OI modifier range is typically ±0.05 to ±0.10 per signal',
      'Orchestrator veto: if the Orchestrator Engine\'s veto is active, ALL signals are suppressed regardless of confluence score',
      'MIL is disabled in backtesting mode (technical_only=True) for reproducibility',
    ],
    caution:
      'MIL data sources (Coinglass OI, funding rates, agent intelligence) have inherent latency and may not reflect the absolute latest market state. The 25% cap provides a structural safeguard, but operators should monitor MIL diagnostics (mil_capped, mil_dominant_source) to ensure non-technical signals are contributing constructively. Frequent capping may indicate MIL data quality issues.',
  },
  {
    id: 'risk_gate',
    title: 'Risk Gate & Trade Approval',
    category: 'Decision Logic',
    badge_bg: 'bg-teal-50',
    badge_text: 'text-teal-600',
    definition:
      'After the ConfluenceScorer generates an OrderCandidate, the Risk Gate applies a comprehensive series of portfolio-level and position-level checks to determine whether the trade should be approved for execution. Candidates that pass all checks are marked as approved; those that fail are rejected with a specific reason.',
    details: [
      'Expected Value (EV) Gate: estimates win probability using a sigmoid model calibrated on score, then computes slippage-adjusted EV. Rejects if normalised EV < 5% of risk. The EV calculation accounts for bid-ask slippage (default 0.05%)',
      'Risk-Reward Floor: rejects any candidate with R:R ratio below 1.0 (reward must at least equal risk)',
      'Portfolio Heat Limit: total open risk exposure across all positions must not exceed 6% of capital',
      'Maximum Concurrent Positions: hard limit of 5 simultaneous open positions',
      'Maximum Drawdown Circuit Breaker: all new trades blocked when portfolio drawdown exceeds 15%',
      'Multi-Timeframe Confirmation: when enabled, rejects candidates whose direction conflicts with the higher-timeframe regime',
      'Crash Defense Integration: position size is scaled down based on the Crash Defense tier (Defensive: 65%, High Alert: 35%, Emergency: 10%, Systemic: 0%)',
      'Spread Filter: rejects candidates when the bid-ask spread exceeds 0.30% of mid price',
    ],
    how_it_works:
      'The Risk Gate processes candidates through a sequential checklist. Each check can either reject the candidate outright (with a recorded reason), reduce the position size, or pass. The checks are ordered from fastest/cheapest to most computationally intensive. A candidate must survive all checks to receive approval. The detailed rejection reason is stored in the candidate\'s rejection_reason field and displayed on the Signal Card.',
    key_points: [
      'Win probability estimation: sigmoid function with configurable steepness (default 8.0) and midpoint (0.55), penalised by up to 15% for uncertain regimes',
      'EV formula: EV = (win_prob × effective_reward) − ((1 − win_prob) × effective_risk), normalised by effective risk',
      'Portfolio heat calculation: Σ(position_size × stop_distance%) / available_capital',
      'All rejection reasons are logged and available in the Scanner diagnostics panel',
    ],
    caution:
      'The Risk Gate is intentionally conservative. It is designed to reject marginal candidates rather than approve them. A high confluence score does not override Risk Gate checks — a candidate with a 0.90 score will still be rejected if portfolio heat is at the limit or drawdown exceeds the circuit breaker. The Risk Gate is the last line of defence before capital is deployed.',
  },
  {
    id: 'position_sizing',
    title: 'Position Sizing',
    category: 'Execution',
    badge_bg: 'bg-amber-50',
    badge_text: 'text-amber-600',
    definition:
      'Position sizing translates the approved OrderCandidate into a specific dollar amount. The system uses risk-based sizing by default: the position size is calculated so that hitting the stop loss results in a loss equal to a fixed percentage of capital. This ensures consistent risk per trade regardless of the asset\'s price or volatility.',
    details: [
      'Risk-based formula: size = (risk_pct × capital) / stop_distance × entry_price',
      'Default risk per trade: 0.5% of capital (Phase 1 demo setting)',
      'Capital percentage cap: 4% maximum per position — prevents any single trade from consuming too much capital regardless of stop distance',
      'Minimum size floor: 10 USDT — prevents sub-minimum exchange orders',
      'Crash Defense scaling: position size is multiplied by the CDA tier factor before execution',
    ],
    how_it_works:
      'The PositionSizer first computes the risk amount in USDT (risk_pct × capital), then divides by the stop distance (|entry − stop|) to determine the quantity, and finally multiplies by the entry price to get the position size in USDT. This size is then clamped between the minimum floor and the capital percentage cap. In Phase 2 (when tiered capital is enabled), the cap adjusts based on the number of open positions and the candidate\'s conviction score.',
    key_points: [
      'Risk per trade and capital cap are independent constraints — both must be satisfied',
      'The risk percentage determines how much capital is at risk if stopped out; the capital cap limits total exposure',
      'Score is not used for sizing — a 0.90 score gets the same size as a 0.56 score (above threshold)',
      'Symbol allocation weights (from Asset Management) adjust the base score for candidate ranking but never influence position sizing',
    ],
    caution:
      'Position sizing assumes the stop loss will be hit at the specified level. In crypto markets, slippage, gaps, and extreme volatility can cause actual losses to exceed the theoretical stop loss amount. The 0.05% slippage adjustment in the EV calculation partially accounts for this, but black-swan events can produce losses well beyond the designed risk per trade.',
  },
  {
    id: 'signal_card',
    title: 'Reading a Signal Card',
    category: 'Interface',
    badge_bg: 'bg-rose-50',
    badge_text: 'text-rose-600',
    definition:
      'Each Signal Card on this page represents one symbol\'s confluence evaluation from the most recent scan cycle. The card displays the symbol, direction, confluence score, contributing models, current regime, and approval status — providing a complete snapshot of the system\'s assessment for that trading pair.',
    details: [
      'Symbol: the trading pair being evaluated (e.g., BTC/USDT)',
      'Direction: ▲ LONG (buy signal) or ▼ SHORT (sell signal) — determined by the weighted directional vote',
      'Confluence Score: the normalised score (0.00–1.00) with a visual progress bar. Higher bars indicate stronger multi-model agreement',
      'Model Tags (blue badges): the specific models that fired and contributed to the score. More tags generally indicate broader consensus',
      'Regime Tag (grey badge): the current market regime classification for this symbol, which influences model weights via the affinity matrix',
      'Approval Status: green "Approved" means the candidate passed all Risk Gate checks and was submitted for execution. Red indicates rejection with the specific reason displayed',
    ],
    how_it_works:
      'Signal Cards are refreshed every 15 seconds via the /signals/confluence API endpoint, with real-time WebSocket updates when available. If the primary endpoint returns no data, the system falls back to the /scanner/pipeline-status endpoint and synthesises signal cards from the pipeline results. Cards for all scanned symbols are shown, including those with score 0 or no signal.',
    key_points: [
      'Cards are sorted by the scan order, not by score — check the score value to compare relative strength',
      'A score of 0.00 with no model tags means no models produced a signal for that symbol in the current regime',
      'The rejection reason provides actionable information about why a candidate was blocked (e.g., "Portfolio heat at limit", "R:R below floor")',
      'Cards update in near real-time — a new scan cycle will refresh all cards simultaneously',
    ],
    caution:
      'Signal Cards show a point-in-time snapshot from the most recent scan cycle. Market conditions can change rapidly between scan cycles. An approved candidate at scan time may no longer represent a valid setup by the time the next scan runs. The system\'s auto-execution logic handles this by validating candidates against live prices before submitting orders.',
  },
];

const CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  'Core Concept':      { bg: 'bg-blue-50',   text: 'text-blue-600' },
  'Scoring Mechanics': { bg: 'bg-purple-50', text: 'text-purple-600' },
  'Decision Logic':    { bg: 'bg-teal-50',   text: 'text-teal-600' },
  'Execution':         { bg: 'bg-amber-50',  text: 'text-amber-600' },
  'Interface':         { bg: 'bg-rose-50',   text: 'text-rose-600' },
};

function ConfluenceReferenceGuide() {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <BookOpen className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">Confluence Signals Reference Guide</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {GUIDE_ENTRIES.map((entry) => {
          const catColor = CATEGORY_COLORS[entry.category] || CATEGORY_COLORS['Core Concept'];
          return (
            <div key={entry.id}>
              <button
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
                className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
              >
                <div className="flex items-center gap-3">
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

                  {/* Two-column: How It Works + Details */}
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">How It Works</h4>
                      <p className="text-sm text-gray-700 leading-relaxed">{entry.how_it_works}</p>
                    </div>
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Technical Details</h4>
                      <ul className="space-y-1.5">
                        {entry.details.map((point, i) => (
                          <li key={i} className="flex gap-2 text-sm text-gray-700">
                            <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                            <span className="leading-relaxed">{point}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  {/* Key Points */}
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Key Points</h4>
                    <ul className="space-y-1.5">
                      {entry.key_points.map((point, i) => (
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
export default function ConfluenceSignals() {
  const { subscribe, lastMessage, status } = useWSStore();
  const queryClient = useQueryClient();

  useEffect(() => { if (status === 'connected') subscribe('signals'); }, [status, subscribe]);
  useEffect(() => {
    const ws = lastMessage['signals'];
    if (ws) queryClient.setQueryData(['confluence-signals'], ws);
  }, [lastMessage, queryClient]);

  const { data: confluenceData } = useQuery({
    queryKey: ['confluence-signals'], queryFn: getConfluenceSignals, refetchInterval: 15000,
  });

  const signals = confluenceData?.signals || [];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <Activity className="w-5 h-5 text-blue-500" />
        <div>
          <h1 className="text-xl font-bold text-gray-900">Confluence Signals</h1>
          <p className="text-sm text-gray-500 mt-0.5">
            Multi-model weighted consensus signals for each tradable pair
          </p>
        </div>
      </div>

      {/* Signal Cards */}
      {signals.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 p-12 text-center">
          <Activity className="w-10 h-10 text-gray-200 mx-auto mb-3" />
          <p className="text-sm font-medium text-gray-500">No active signals</p>
          <p className="text-xs text-gray-400 mt-1">Confluence signals will appear after the next scan cycle completes</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {signals.map((sig, i) => <SignalCard key={`${sig.symbol}-${i}`} signal={sig} />)}
        </div>
      )}

      {/* Reference Guide */}
      <ConfluenceReferenceGuide />
    </div>
  );
}
