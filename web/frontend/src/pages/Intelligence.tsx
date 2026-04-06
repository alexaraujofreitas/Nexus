import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { BrainCircuit, BookOpen, ChevronDown } from 'lucide-react';
import { getAgentStatuses } from '../api/signals';
import { cn } from '../lib/utils';

// ── AI Agents definition (actual intelligence agents, not signal models) ──
const AI_AGENTS = [
  // ── All 23 Agents Enabled ──
  { name: 'Funding Rate Agent', key: 'funding_rate', category: 'Market Microstructure', description: 'Monitors perpetual funding rates across exchanges for sentiment divergence', enabled: true },
  { name: 'News Agent', key: 'news', category: 'Sentiment', description: 'Processes crypto news feeds (CryptoPanic, RSS) via FinBERT + VADER NLP for market impact scoring', enabled: true },
  { name: 'Macro Agent', key: 'macro', category: 'Macro', description: 'Tracks macro indicators (DXY, bond yields, VIX, S&P 500, gold) for risk-on/risk-off signals', enabled: true },
  { name: 'Geopolitical Agent', key: 'geopolitical', category: 'Macro', description: 'Monitors geopolitical events and sanctions risk affecting crypto markets', enabled: true },
  { name: 'On-Chain Agent', key: 'onchain', category: 'On-Chain', description: 'Tracks exchange deposits/withdrawals, net position changes, and accumulation/distribution patterns', enabled: true },
  { name: 'Whale Tracking Agent', key: 'whale', category: 'On-Chain', description: 'Detects large wallet transfers and exchange inflow/outflow anomalies', enabled: true },
  { name: 'Liquidation Flow Agent', key: 'liquidation_flow', category: 'Market Microstructure', description: 'Monitors liquidation cascades and open interest changes via Coinglass', enabled: true },
  { name: 'Crash Detection Agent', key: 'crash_detection', category: 'Risk', description: 'Multi-component crash scorer with 7 risk factors and 4-tier response', enabled: true },
  { name: 'Squeeze Detection Agent', key: 'squeeze_detection', category: 'Market Microstructure', description: 'Detects short/long squeeze setups from funding + OI + price divergence', enabled: true },
  { name: 'Stablecoin Liquidity Agent', key: 'stablecoin', category: 'Liquidity', description: 'Tracks USDT/USDC supply changes as leading indicators of capital flow', enabled: true },
  { name: 'Position Monitor Agent', key: 'position_monitor', category: 'Execution', description: 'Watches open positions for stop/target proximity and exit timing', enabled: true },
  { name: 'Telegram Sentiment Agent', key: 'telegram', category: 'Sentiment', description: 'Monitors Telegram crypto channels for whale alerts, pump signals, and trading sentiment', enabled: true },
  { name: 'Order Book Agent', key: 'order_book', category: 'Market Microstructure', description: 'Analyzes bid/ask depth and order book imbalances across 20 watchlist symbols', enabled: true },
  { name: 'Options Flow Agent', key: 'options_flow', category: 'Derivatives', description: 'Monitors options open interest and max pain for BTC/ETH directional bias', enabled: true },
  { name: 'Volatility Surface Agent', key: 'volatility_surface', category: 'Derivatives', description: 'Tracks implied volatility skew and term structure changes', enabled: true },
  { name: 'Social Sentiment Agent', key: 'social_sentiment', category: 'Sentiment', description: 'Aggregates sentiment from Twitter, Reddit, and Telegram communities', enabled: true },
  { name: 'Sector Rotation Agent', key: 'sector_rotation', category: 'Macro', description: 'Tracks sector ETF momentum (XLK, QQQ, ARKK, GLD, TLT, VIX) for risk-on/off rotation signals', enabled: true },
  { name: 'Narrative Shift Agent', key: 'narrative_shift', category: 'Sentiment', description: 'Detects narrative regime shifts in crypto media discourse', enabled: true },
  { name: 'Miner Flow Agent', key: 'miner_flow', category: 'On-Chain', description: 'Tracks Bitcoin miner wallet outflows as selling pressure indicator', enabled: true },
  { name: 'Twitter Sentiment Agent', key: 'twitter', category: 'Sentiment', description: 'Real-time Twitter/X sentiment scoring for crypto assets via VADER + keyword analysis', enabled: true },
  { name: 'Reddit Sentiment Agent', key: 'reddit', category: 'Sentiment', description: 'Monitors Reddit crypto communities (r/cryptocurrency, r/bitcoin) for sentiment shifts', enabled: true },
  { name: 'Scalping Agent', key: 'scalp', category: 'Execution', description: 'Short-timeframe scalping signal generation for quick trades', enabled: true },
  { name: 'Liquidity Vacuum Agent', key: 'liquidity_vacuum', category: 'Market Microstructure', description: 'Detects liquidity voids and stop-hunt zones in the order book', enabled: true },
];

const CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  'Market Microstructure': { bg: 'bg-indigo-50', text: 'text-indigo-600' },
  'Sentiment': { bg: 'bg-violet-50', text: 'text-violet-600' },
  'Macro': { bg: 'bg-sky-50', text: 'text-sky-600' },
  'On-Chain': { bg: 'bg-emerald-50', text: 'text-emerald-600' },
  'Risk': { bg: 'bg-red-50', text: 'text-red-600' },
  'Liquidity': { bg: 'bg-cyan-50', text: 'text-cyan-600' },
  'Execution': { bg: 'bg-orange-50', text: 'text-orange-600' },
  'Derivatives': { bg: 'bg-purple-50', text: 'text-purple-600' },
};

// ── Live status helpers (matching Dashboard logic) ──
function getAgentDot(live: any): { dot: string; label: string; labelColor: string } {
  if (!live) return { dot: 'bg-gray-400', label: 'No data', labelColor: 'text-gray-500' };
  if (live.errors > 0) return { dot: 'bg-red-500', label: `${live.errors} error${live.errors > 1 ? 's' : ''}`, labelColor: 'text-red-600' };
  if (live.stale) return { dot: 'bg-amber-500', label: 'Stale', labelColor: 'text-amber-600' };
  // Green = running + has fresh data + contributing to orchestrator/dependent model
  if (live.running && live.has_data) return { dot: 'bg-green-500', label: 'Active', labelColor: 'text-green-600' };
  // Running but no data — agent is alive but not contributing
  if (live.running) return { dot: 'bg-blue-400', label: 'Running', labelColor: 'text-blue-500' };
  return { dot: 'bg-gray-400', label: 'Idle', labelColor: 'text-gray-500' };
}

// ── Agent Card ──
function AgentCard({ agent, live }: { agent: typeof AI_AGENTS[0]; live: any }) {
  const { dot, label, labelColor } = agent.enabled
    ? getAgentDot(live)
    : { dot: 'bg-gray-300', label: 'Disabled', labelColor: 'text-gray-400' };
  const catColor = CATEGORY_COLORS[agent.category] || { bg: 'bg-blue-50', text: 'text-blue-600' };

  return (
    <div className={cn(
      'bg-white rounded-xl border p-4 transition-colors',
      agent.enabled ? 'border-gray-200' : 'border-gray-100 opacity-60',
    )}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-semibold text-sm text-gray-900">{agent.name}</span>
        <div className="flex items-center gap-1.5">
          <span className={cn('w-2 h-2 rounded-full', dot)} />
          <span className={cn('text-xs font-medium', labelColor)}>{label}</span>
        </div>
      </div>
      <span className={cn('inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wider mb-2', catColor.bg, catColor.text)}>
        {agent.category}
      </span>
      <p className="text-xs text-gray-500 leading-relaxed">{agent.description}</p>
    </div>
  );
}

// ── Reference Guide Data ──
interface AgentGuideEntry {
  id: string;
  title: string;
  category: string;
  definition: string;
  dataSource: string;
  tradeImpact: string[];
  enabled: boolean;
}

const AGENT_GUIDE: AgentGuideEntry[] = [
  // ── Status Colors (special entry) ──
  {
    id: 'status-colors',
    title: 'Agent Status Indicators',
    category: 'Reference',
    definition: 'Each AI agent displays a colored status dot reflecting its real-time health, derived from the live API. The status is polled every 30 seconds.',
    dataSource: 'Live agent status API (/signals/agents)',
    tradeImpact: [
      'Green (Active) — Agent is running, data is fresh, no errors reported. The agent\'s signal is contributing to the orchestrator vote and/or its dependent signal model.',
      'Blue (Running) — Agent thread is alive but has no data to contribute (has_data=false). Common reasons: API credentials missing, no source configured, dependent service offline (e.g., WebSocket disabled). Not included in orchestrator vote.',
      'Amber (Stale) — Agent is running but its data hasn\'t updated within the expected refresh interval. The orchestrator applies a staleness decay, reducing this agent\'s weight toward zero over time.',
      'Red (Errors) — Agent has encountered one or more errors (API failures, parse errors, timeout). Its signal is excluded from the orchestrator vote until errors clear.',
      'Gray (No data / Idle) — No live data has been received from this agent, or the agent is awaiting its first scheduled run. Treated as inactive by the orchestrator.',
      'Card-level warning: When < 60% of enabled agents are healthy the Dashboard card border turns amber; when < 25% are healthy (or > 50% have errors) it turns red. This signals degraded intelligence quality that can reduce the orchestrator\'s confluence contribution by up to 0.10 points.',
    ],
    enabled: true,
  },
  // ── Enabled Agents ──
  {
    id: 'funding-rate',
    title: 'Funding Rate Agent',
    category: 'Market Microstructure',
    definition: 'Monitors perpetual futures funding rates across exchanges. Funding rate is the periodic payment between long and short holders — positive rates mean longs pay shorts (bullish crowding), negative rates mean shorts pay longs (bearish crowding).',
    dataSource: 'CCXT exchange API, polled every 5 minutes. Covers all watchlist symbols on the configured exchange (Bybit Demo).',
    tradeImpact: [
      'Highest orchestrator weight (0.184 default). Directly feeds meta-signal as a market microstructure vote.',
      'Neutral-zone micro-signal: computes -rate_pct × 5.0, so even mild funding skew produces a proportional directional signal.',
      'Extreme negative funding combined with bearish macro signals can trigger the orchestrator\'s macro veto, blocking all long trades.',
      'Also feeds the Crash Detection Agent as one of its 7 risk components.',
    ],
    enabled: true,
  },
  {
    id: 'news',
    title: 'News Agent',
    category: 'Sentiment',
    definition: 'Collects and scores crypto news from 5 RSS sources and the CryptoPanic v2 API. Headlines are processed through both FinBERT (GPU-accelerated transformer, ~5-10ms/batch on RTX 4070) and VADER for dual-scored sentiment. FinBERT is integrated directly in this agent — there is no separate FinBERT agent.',
    dataSource: 'CryptoPanic API (/api/developer/v2/posts/), 5 RSS feeds. Primary window: last 8 hours, fallback: 24 hours if primary is empty. Polled every 15 minutes.',
    tradeImpact: [
      'Orchestrator weight: 0.039. Feeds meta-signal as a sentiment-informed directional vote.',
      'Computes micro-signal from positive/negative article skew with confidence floor 0.30.',
      'Strong negative sentiment pushes the orchestrator meta-signal bearish, weakening long setups.',
      'News impact decays over time — a headline from 6 hours ago has less weight than one from 30 minutes ago.',
    ],
    enabled: true,
  },
  {
    id: 'macro',
    title: 'Macro Agent',
    category: 'Macro',
    definition: 'Tracks traditional macro indicators that historically correlate with crypto risk appetite: Dollar Index (DXY), US Treasury yields, VIX (volatility index), S&P 500 momentum, Fear & Greed Index (FNG), and gold prices.',
    dataSource: 'Financial data APIs and RSS feeds. Polled every 1 hour (macro conditions change slowly).',
    tradeImpact: [
      'Orchestrator weight: 0.126. Contributes to the meta-signal as a weighted vote in the macro/risk-off dimension.',
      'A strongly bearish macro signal (< -0.4) combined with macro_risk_score > 0.75 triggers the orchestrator\'s macro veto — blocking all long entries.',
      'Computes proportional micro-signals from FNG, DXY, yield curve, and equity momentum individually.',
      'Macro signal is regime-agnostic — it provides context regardless of whether the technical regime is bull, bear, or ranging.',
    ],
    enabled: true,
  },
  {
    id: 'geopolitical',
    title: 'Geopolitical Agent',
    category: 'Macro',
    definition: 'Monitors geopolitical events — sanctions, regulatory announcements, political instability, and conflict escalation — that can cause sudden crypto market dislocations.',
    dataSource: 'News feeds and CryptoPanic API, filtered for geopolitical keywords. Polled every 1 hour. Publishes via Topics.SOCIAL_SIGNAL (source="geopolitical").',
    tradeImpact: [
      'Orchestrator weight: 0.020. Contributes to the meta-signal as a risk-off assessment.',
      'Base confidence 0.30; NEUTRAL risk level returns mild positive signal (+0.05).',
      'Sudden geopolitical shocks can spike the Crash Detection Agent\'s score, triggering defensive actions.',
    ],
    enabled: true,
  },
  {
    id: 'onchain',
    title: 'On-Chain Agent',
    category: 'On-Chain',
    definition: 'Tracks blockchain-level activity: exchange deposits/withdrawals, net position changes, and accumulation/distribution patterns visible only on-chain.',
    dataSource: 'On-chain analytics APIs. Polled every 1 hour. Covers BTC and ETH primary chains.',
    tradeImpact: [
      'Orchestrator weight: 0.074. Contributes to the meta-signal as a weighted vote.',
      'Computes micro-signal from price momentum (momentum × 0.03) when on-chain data is in neutral range.',
      'Large net exchange inflows are interpreted as selling pressure (bearish); large withdrawals suggest accumulation (bullish).',
      'On-chain data is slow-moving (hours to days) — it provides a directional bias, not timing signals.',
    ],
    enabled: true,
  },
  {
    id: 'whale',
    title: 'Whale Tracking Agent',
    category: 'On-Chain',
    definition: 'Detects anomalously large wallet transfers and exchange inflow/outflow events that indicate institutional or whale-level positioning changes.',
    dataSource: 'On-chain analytics APIs and whale alert feeds. Polled every 1 hour.',
    tradeImpact: [
      'A cluster of large exchange inflows is a leading indicator of selling pressure — strengthens the bearish orchestrator vote.',
      'Large withdrawals to cold storage suggest long-term accumulation — strengthens the bullish vote.',
      'Works in tandem with the On-Chain Agent — whale activity is the high-amplitude subset of general on-chain flow.',
      'Contributes to orchestrator meta-signal only. Does not directly feed signal sub-models.',
    ],
    enabled: true,
  },
  {
    id: 'liquidation-flow',
    title: 'Liquidation Flow Agent',
    category: 'Market Microstructure',
    definition: 'Monitors liquidation cascades and open interest (OI) changes across perpetual futures markets. Liquidations are forced position closures that create rapid price moves and can cascade.',
    dataSource: 'Coinglass API (5-minute TTL cache with RLock). Tracks OI changes, liquidation volumes, and long/short ratios.',
    tradeImpact: [
      'Orchestrator weight: 0.039. Feeds the MIL modifier in the ConfluenceScorer — OI divergence adjusts the technical baseline score.',
      'A cascade of long liquidations during a downtrend strengthens short signals and weakens long signals.',
      'Rising OI with rising price confirms trend strength — falling OI with rising price warns of a squeeze.',
      'Also feeds the Crash Detection Agent as a high-weight risk component (liquidation cascades are the primary crash propagation mechanism).',
    ],
    enabled: true,
  },
  {
    id: 'crash-detection',
    title: 'Crash Detection Agent',
    category: 'Risk',
    definition: 'The core risk monitoring agent. Aggregates 7 independent risk factors into a composite crash score (0–10) that drives the 4-tier Crash Defense response system. Score of 0 maps to a mild positive signal (+0.10) — "no crash risk detected" is useful information.',
    dataSource: 'Aggregates signals from: Funding Rate, Liquidation Flow, price momentum, volume spikes, OI changes, cross-asset correlation breakdown, and stablecoin flows. Recalculated continuously.',
    tradeImpact: [
      'Orchestrator weight: 0.068. Confidence floor 0.40 ensures this agent always contributes when data is available.',
      'Score < 5.0 (NORMAL): No protective action. Mild positive signal boosts long conviction.',
      'Score ≥ 5.0 (DEFENSIVE): All open long positions moved to breakeven stop loss (auto_execute required).',
      'Score ≥ 7.0 (HIGH ALERT): 50% partial close on all longs. Score ≥ 8.0 (EMERGENCY): All longs closed.',
      'Score ≥ 9.0 (SYSTEMIC): ALL positions (longs and shorts) closed immediately.',
      'This is the only agent that can autonomously execute trades via crash_defense.auto_execute config gate.',
    ],
    enabled: true,
  },
  {
    id: 'squeeze-detection',
    title: 'Squeeze Detection Agent',
    category: 'Market Microstructure',
    definition: 'Detects conditions ripe for short squeezes (or long squeezes): extreme funding rate skew + elevated OI + price divergence from funding direction. Squeezes produce violent counter-trend moves.',
    dataSource: 'Combines data from Funding Rate Agent + Liquidation Flow Agent + price action. Polled every 5 minutes.',
    tradeImpact: [
      'A short squeeze signal (negative funding + rising price + high short OI) strengthens long confluence and can push marginal setups over the threshold.',
      'A long squeeze signal (extreme positive funding + falling price) strengthens short signals.',
      'Contributes to the orchestrator meta-signal. Does not directly feed signal sub-models.',
      'Squeeze signals are high-conviction but rare — they fire 2–5 times per month on average.',
    ],
    enabled: true,
  },
  {
    id: 'stablecoin',
    title: 'Stablecoin Liquidity Agent',
    category: 'Liquidity',
    definition: 'Tracks USDT and USDC supply changes as a leading indicator of capital inflows/outflows to the crypto market. Rising stablecoin supply = dry powder waiting to buy.',
    dataSource: 'Stablecoin supply APIs and on-chain minting/burning data. Polled every 1 hour.',
    tradeImpact: [
      'Rising stablecoin supply is interpreted as potential buying pressure (capital is entering the ecosystem), pushing the orchestrator signal bullish.',
      'Falling supply or large redemptions signal capital exodus — weakens long conviction.',
      'This is a slow-moving macro indicator (days to weeks) that provides background directional bias, not trade timing.',
      'Contributes to orchestrator meta-signal only.',
    ],
    enabled: true,
  },
  {
    id: 'position-monitor',
    title: 'Position Monitor Agent',
    category: 'Execution',
    definition: 'Watches all open positions for stop/target proximity, time-based exit conditions, and dynamic exit adjustments. Unlike other agents, this one monitors the portfolio rather than the market.',
    dataSource: 'Internal PaperExecutor position data. Runs continuously alongside the tick loop.',
    tradeImpact: [
      'Does not contribute to the confluence score or orchestrator vote — it monitors positions after entry, not before.',
      'Tracks unrealized P&L evolution, drawdown from peak, and time in trade for exit timing decisions.',
      'Works with the v1.2 partial exit system: monitors whether the 1R partial close (33% at 1R) has been triggered and breakeven SL applied.',
      'Publishes position health alerts via the event bus for the Dashboard and Notifications system.',
    ],
    enabled: true,
  },
  {
    id: 'telegram',
    title: 'Telegram Sentiment Agent',
    category: 'Sentiment',
    definition: 'Monitors Telegram crypto channels for whale alerts, pump signals, and trading sentiment. Uses 4-tier data sources: Telegram Bot API (if configured), Telemetrio public stats, t.me preview page scraping, and graceful degradation.',
    dataSource: 'Telegram channels: @whale_alert_io, @cryptowhale, @crypto_pump, @bitcoin_signal. Polled every 20 minutes. Publishes via Topics.TELEGRAM_SIGNAL.',
    tradeImpact: [
      'Contributes to social sentiment aggregation alongside Twitter and Reddit agents.',
      'Whale alert channels provide leading indicators of large position changes.',
      'Pump signal channels are contrarian at extremes — high activity often precedes reversals.',
    ],
    enabled: true,
  },
  // ── Additional Agents ──
  {
    id: 'order-book',
    title: 'Order Book Agent',
    category: 'Market Microstructure',
    definition: 'Analyzes real-time bid/ask depth to detect order book imbalances, spoofing patterns, and large resting orders that indicate support/resistance levels.',
    dataSource: 'Exchange WebSocket order book streams (currently disabled: WebSocket disabled in config to prevent Qt crash at 10Hz).',
    tradeImpact: [
      'Orchestrator weight: 0.168 (second highest). Contributes strongly to meta-signal as an order flow confirmation layer.',
      'Actively polling 20 symbols via REST. OrderBookModel is archived (structural TF gate), but agent feeds orchestrator directly.',
      'Produces avg_signal and avg_conf across all watchlist symbols every ~30 seconds.',
    ],
    enabled: true,
  },
  {
    id: 'options-flow',
    title: 'Options Flow Agent',
    category: 'Derivatives',
    definition: 'Monitors crypto options markets for open interest distribution, max pain levels, and unusual options activity that can predict directional moves.',
    dataSource: 'Options analytics APIs (Deribit, exchange options data). Not currently connected.',
    tradeImpact: [
      'Orchestrator weight: 0.137 (third highest). Contributes strongly as a derivatives-informed directional bias.',
      'Actively monitoring BTC and ETH options flow. Produces per-asset directional signals.',
      'Max pain analysis predicts price magnets near options expiry dates.',
    ],
    enabled: true,
  },
  {
    id: 'social-sentiment',
    title: 'Social Sentiment Agent',
    category: 'Sentiment',
    definition: 'Aggregates sentiment signals from Twitter, Reddit, and Telegram crypto communities to detect retail sentiment shifts and hype cycles. Publishes via Topics.SOCIAL_SIGNAL (source="social_sentiment").',
    dataSource: 'Aggregates from Twitter, Reddit, Telegram sub-agents + Fear & Greed Index. Computes FNG offset (fng_norm × 0.18) as micro-signal.',
    tradeImpact: [
      'Orchestrator weight: 0.068. Contributes to meta-signal as a social sentiment vote via FNG micro-signal.',
      'Retail sentiment is contrarian at extremes — extreme bullish retail sentiment often precedes local tops.',
      'Aggregates Twitter, Reddit, Telegram sub-agent data + Fear & Greed Index offset.',
    ],
    enabled: true,
  },
  {
    id: 'volatility-surface',
    title: 'Volatility Surface Agent',
    category: 'Derivatives',
    definition: 'Tracks the implied volatility surface — skew, term structure, and smile dynamics — across crypto options markets for regime change signals.',
    dataSource: 'Options exchange APIs (Deribit). Not currently connected.',
    tradeImpact: [
      'Orchestrator weight: 0.068. IV skew steepening indicates market stress or directional fear.',
      'Actively monitoring 2 instruments. Producing avg_signal and avg_conf with strong confidence (~0.75).',
      'Term structure inversion (near > far) signals expected short-term volatility spike.',
    ],
    enabled: true,
  },
  {
    id: 'sector-rotation',
    title: 'Sector Rotation Agent',
    category: 'Macro',
    definition: 'Tracks macro sector ETF momentum and capital rotation patterns to determine whether institutional money is flowing toward or away from crypto/risk assets. Monitors Risk-ON ETFs (XLK, QQQ, ARKK) and Risk-OFF ETFs (GLD, TLT, VIX, XLU) via yfinance 5-day momentum, plus BTC dominance from CoinGecko.',
    dataSource: 'yfinance sector ETF data + CoinGecko BTC dominance. Polled every 1 hour. Publishes via Topics.SOCIAL_SIGNAL (source="sector_rotation").',
    tradeImpact: [
      'Orchestrator weight: 0.009 (lowest). Risk-ON sectors rising = bullish crypto; Risk-OFF rising = bearish.',
      'BTC dominance trend informs alt-coin rotation signals.',
      'Orchestrator weight is lowest (0.009) — acts as a tiebreaker bias rather than a primary signal source.',
    ],
    enabled: true,
  },
  {
    id: 'narrative-shift',
    title: 'Narrative Shift Agent',
    category: 'Sentiment',
    definition: 'Detects narrative regime shifts in crypto media — when the dominant story changes (e.g., "crypto winter" to "institutional adoption" to "DeFi summer"), it signals a macro sentiment pivot.',
    dataSource: 'NLP analysis of long-form crypto media, blog posts, and research reports.',
    tradeImpact: [
      'Narrative shifts are ultra-slow (weeks to months) but powerful — they set the backdrop for all other signals.',
      'Feeds the orchestrator as a long-term directional bias layer.',
      'Requires advanced NLP pipeline for long-form content analysis — signals update slowly (days/weeks).',
    ],
    enabled: true,
  },
  {
    id: 'miner-flow',
    title: 'Miner Flow Agent',
    category: 'On-Chain',
    definition: 'Tracks Bitcoin miner wallet outflows. Miners are forced sellers (operational costs) and their selling patterns provide insight into supply pressure and market cycle positioning.',
    dataSource: 'On-chain analytics tracking known miner wallets and pool addresses.',
    tradeImpact: [
      'Elevated miner selling during price weakness signals capitulation — historically a bottom indicator.',
      'Low miner selling during price strength suggests miners are HODLing — confirms bull trend.',
      'BTC-specific — not applicable to altcoins. Relies on on-chain analytics API for miner wallet tracking.',
    ],
    enabled: true,
  },
  {
    id: 'twitter',
    title: 'Twitter Sentiment Agent',
    category: 'Sentiment',
    definition: 'Real-time sentiment scoring of crypto-related tweets from key opinion leaders and trending topics on Twitter/X.',
    dataSource: 'Twitter/X API. Not currently connected (API key required).',
    tradeImpact: [
      'Would provide real-time retail sentiment pulse alongside the Social Sentiment Agent.',
      'Crypto Twitter is often a leading indicator of narrative shifts and hype cycles.',
      'Uses VADER + 40 crypto-domain keyword boosters for scoring. Requires Twitter API credentials for live data.',
    ],
    enabled: true,
  },
  {
    id: 'reddit',
    title: 'Reddit Sentiment Agent',
    category: 'Sentiment',
    definition: 'Monitors crypto subreddits (r/cryptocurrency, r/bitcoin, etc.) for sentiment shifts, trending topics, and unusual activity spikes.',
    dataSource: 'Reddit API (Client ID + Secret configured in vault). Not currently activated.',
    tradeImpact: [
      'Reddit sentiment is useful for detecting retail euphoria or panic at extremes.',
      'Unusual post volume spikes can precede significant price moves (historically ~2–6 hours lead).',
      'Uses VADER + crypto keyword boosters for scoring. API credentials required for live subreddit monitoring.',
    ],
    enabled: true,
  },
  {
    id: 'scalp',
    title: 'Scalping Agent',
    category: 'Execution',
    definition: 'Generates short-timeframe scalping signals for sub-30-minute trades based on order flow, micro-structure, and momentum bursts.',
    dataSource: 'Would require WebSocket tick data and order book depth (both currently disabled).',
    tradeImpact: [
      'Generates separate scalp-type signals outside the main 30-minute scan pipeline.',
      'Requires WebSocket enablement and a dedicated execution path with tighter risk parameters.',
      'Best suited for sub-30m timeframes — operates independently from the primary IDSS scan cycle.',
    ],
    enabled: true,
  },
  {
    id: 'liquidity-vacuum',
    title: 'Liquidity Vacuum Agent',
    category: 'Market Microstructure',
    definition: 'Detects liquidity voids — price levels with unusually thin order book depth where price can move rapidly through — and stop-hunt zones where clusters of stop losses are likely resting.',
    dataSource: 'Would require real-time order book depth data via WebSocket.',
    tradeImpact: [
      'Liquidity voids above or below price can be targets for explosive moves — useful for setting take-profit levels.',
      'Stop-hunt zone detection would help place stops beyond manipulation-prone levels.',
      'Requires WebSocket and order book depth analysis for real-time liquidity gap detection.',
    ],
    enabled: true,
  },
];

const GUIDE_CATEGORY_COLORS: Record<string, { bg: string; text: string }> = {
  'Reference': { bg: 'bg-gray-100', text: 'text-gray-700' },
  'Market Microstructure': { bg: 'bg-indigo-100', text: 'text-indigo-700' },
  'Sentiment': { bg: 'bg-violet-100', text: 'text-violet-700' },
  'Macro': { bg: 'bg-sky-100', text: 'text-sky-700' },
  'On-Chain': { bg: 'bg-emerald-100', text: 'text-emerald-700' },
  'Risk': { bg: 'bg-red-100', text: 'text-red-700' },
  'Liquidity': { bg: 'bg-cyan-100', text: 'text-cyan-700' },
  'Execution': { bg: 'bg-orange-100', text: 'text-orange-700' },
  'Derivatives': { bg: 'bg-purple-100', text: 'text-purple-700' },
};

function AgentReferenceGuide() {
  const [open, setOpen] = useState<string | null>(null);

  return (
    <div className="bg-white rounded-xl border border-gray-200 overflow-hidden">
      <div className="px-5 py-3.5 border-b border-gray-100 flex items-center gap-2">
        <BookOpen className="w-4 h-4 text-blue-500" />
        <h2 className="text-sm font-bold text-gray-900">AI Agents Reference Guide</h2>
      </div>
      <div className="divide-y divide-gray-100">
        {AGENT_GUIDE.map((entry) => {
          const catColor = GUIDE_CATEGORY_COLORS[entry.category] || GUIDE_CATEGORY_COLORS['Reference'];
          return (
            <div key={entry.id}>
              <button
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
                className="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-50/50 transition-colors"
              >
                <div className="flex items-center gap-3">
                  <span className={cn(
                    'w-2.5 h-2.5 rounded-full shrink-0',
                    entry.enabled ? 'bg-green-500' : entry.id === 'status-colors' ? 'bg-blue-500' : 'bg-gray-300',
                  )} />
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
                  <div>
                    <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Definition</h4>
                    <p className="text-sm text-gray-700 leading-relaxed">{entry.definition}</p>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Data Source</h4>
                      <p className="text-sm text-gray-700 leading-relaxed">{entry.dataSource}</p>
                    </div>
                    <div>
                      <h4 className="text-[10px] font-bold text-gray-400 uppercase tracking-wider mb-1.5">Impact on Trade Decisions</h4>
                      <ul className="space-y-1.5">
                        {entry.tradeImpact.map((point, i) => (
                          <li key={i} className="flex gap-2 text-sm text-gray-700">
                            <span className="text-blue-400 mt-1 shrink-0">&#x2022;</span>
                            <span className="leading-relaxed">{point}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  </div>

                  {!entry.enabled && entry.id !== 'status-colors' && (
                    <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3">
                      <p className="text-xs font-semibold text-amber-800 mb-1">Currently Disabled</p>
                      <p className="text-sm text-amber-700 leading-relaxed">
                        This agent is disabled in configuration. It can be re-enabled by setting <code className="text-xs bg-amber-100 px-1 rounded">agents.{entry.id.replace(/-/g, '_')}_enabled: true</code> in config.yaml without restarting.
                      </p>
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── AI Agents Page ────────────────────────────────────────
export default function Intelligence() {
  const { data: agentData } = useQuery({ queryKey: ['agent-statuses'], queryFn: getAgentStatuses, refetchInterval: 30000 });

  const enabledAgents = AI_AGENTS.filter(a => a.enabled);
  const disabledAgents = AI_AGENTS.filter(a => !a.enabled);

  // Active count = only agents truly contributing (running + has_data + no errors + not stale)
  const activeCount = enabledAgents.filter(a => {
    const live = agentData?.[a.key];
    return live?.running && live?.has_data && !live?.stale && !(live?.errors > 0);
  }).length;

  const healthRatio = enabledAgents.length > 0 ? activeCount / enabledAgents.length : 0;
  const severity: 'red' | 'amber' | 'green' =
    healthRatio < 0.25 ? 'red' : healthRatio < 0.60 ? 'amber' : 'green';
  const badgeColor = { red: 'bg-red-100 text-red-700', amber: 'bg-amber-100 text-amber-700', green: 'bg-blue-100 text-blue-700' }[severity];

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center gap-3">
        <BrainCircuit className="w-5 h-5 text-blue-500" />
        <h1 className="text-xl font-bold text-gray-900">AI Agents</h1>
        <span className={cn('px-2 py-0.5 rounded text-xs font-bold', badgeColor)}>
          {activeCount}/{enabledAgents.length} active
        </span>
      </div>

      {/* Active Agents */}
      <div>
        <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Enabled Agents</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {enabledAgents.map((agent) => <AgentCard key={agent.key} agent={agent} live={agentData?.[agent.key]} />)}
        </div>
      </div>

      {/* Disabled Agents (only shown if any exist) */}
      {disabledAgents.length > 0 && (
        <div>
          <h2 className="text-[11px] font-semibold text-gray-900 uppercase tracking-wider mb-3">Disabled Agents</h2>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
            {disabledAgents.map((agent) => <AgentCard key={agent.key} agent={agent} live={agentData?.[agent.key]} />)}
          </div>
        </div>
      )}

      {/* Reference Guide */}
      <AgentReferenceGuide />

    </div>
  );
}
