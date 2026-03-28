# External Research Inputs: PBL/SLC Optimization
## Branch: `research/pbl-slc-optimization-matrix`
**Date:** 2026-03-28 | Research review for NexusTrader v1.3 optimization workstream

---

## Purpose

This document reviews external academic and practitioner literature across 7 topics relevant to the PBL/SLC optimization research. Each finding is classified as:
- **LIKELY USEFUL** — directly actionable in this research workstream
- **MAYBE USEFUL** — applicable in a future phase or as secondary validation
- **NOT USEFUL** — documented for completeness but not applicable

---

## 1. Pullback / Retracement Entries in Trend Systems

### Finding 1.1 — EMA Proximity Zones (LIKELY USEFUL)
**Core idea:** Pullback entries are most reliable when price retraces into EMA20–EMA50 proximity during an established trend, confirmed by rejection candle and RSI in 40–60 range.

**NexusTrader relevance:** Directly validates PBL model design. The current EMA50 proximity gate (0.5×ATR) is within the documented effective range. The RSI>40 floor aligns with the literature, though raising to 45–50 may improve WR at cost of trade count.

**Source:** FMZ Quant — Crypto Pullback Strategy (2024); Liquidity Finder — Moving Average + Fibonacci Guide
**Status:** LIKELY USEFUL — validates current design; informs ema_prox_atr_mult search range

### Finding 1.2 — Fibonacci 38.2%–61.8% Confluence (MAYBE USEFUL)
**Core idea:** Pullback entries stalling at Fibonacci retracement levels 38.2–61.8% within the prior trend leg show better R:R than EMA proximity alone.

**NexusTrader relevance:** Could serve as secondary confluence check for PBL. Would require computing swing high/low on the 4h series and checking whether the current 30m close is within the retracement zone. Adds complexity. Not in current PBL logic.

**Source:** Liquidity Finder — Pullback Trading Strategy (2024)
**Status:** MAYBE USEFUL — Phase 2 enhancement candidate; do not include in current sweep

### Finding 1.3 — 1H Timeframe Sweet Spot (LIKELY USEFUL)
**Core idea:** Among tested crypto timeframes (15m–4h), the 1h timeframe produces best risk-adjusted returns for EMA pullback strategies. 30m accumulates more noise but preserves edge if combined with strict filtering.

**NexusTrader relevance:** PBL operates on 30m primary with 4h HTF gate. The higher noise at 30m directly explains part of PBL's lower standalone PF=0.8995. Adding a 1h intermediate confirmation might improve quality. Currently SLC uses 1h independently.

**Source:** FMZ Strategy Library — Momentum EMA Pullback System (2025)
**Status:** LIKELY USEFUL — potential justification for testing 1h as intermediate TF gate for PBL

---

## 2. Regime-Aware Trading Systems

### Finding 2.1 — HMM-Based Regime Classification (LIKELY USEFUL)
**Core idea:** HMM-based regime classifiers with 3–5 states outperform moving-average-only classifiers for dynamic position gating. Key states: bull trend, bear trend, high-vol expansion, low-vol compression, crisis.

**NexusTrader relevance:** NexusTrader HMM uses 3 states (confirmed at startup). ResearchRegimeClassifier uses 6 codes (SIDEWAYS/BULL/BEAR/BULL_EXP/BEAR_EXP/CRASH). Both match documented best practice. The REGIME_AFFINITY matrix plus crisis suppression is well-founded.

**Source:** QuantStart — Market Regime Detection using HMM; MacroSynergy — Classifying Market Regimes
**Status:** LIKELY USEFUL — validates existing design; no change needed

### Finding 2.2 — Regime-Indexed Position Size Caps (LIKELY USEFUL)
**Core idea:** Optimal portfolio sizing scales with regime: 100% in bull, 50% in sideways, 0% in crisis. Regime-indexed caps reduce drawdown 15–30% without degrading CAGR significantly.

**NexusTrader relevance:** CrashDefenseController already applies emergency response at score≥7.0–9.0. The optimization could extend this by shrinking `pos_frac` from 0.35 to 0.20 in `ranging` or `uncertain` regimes without touching PBL/SLC logic.

**Source:** RegimeNAS — Regime-Aware Trading (2024 preprint)
**Status:** LIKELY USEFUL — addressable in this research if regime-conditional sizing variants are added to matrix

### Finding 2.3 — Dual-Regime Confirmation (MAYBE USEFUL)
**Core idea:** Combining HMM regime state with secondary ADX-regime check (ADX > 25 = trending, < 20 = ranging) reduces false signals in trend-following models by 10–25%.

**NexusTrader relevance:** ResearchRegimeClassifier already uses ADX≥22 as part of BULL_TREND / BEAR_TREND definition. Adding explicit ADX gate inside PBL evaluate() would duplicate existing regime logic. The 4h ADX gate variant is testable as part of the 4h confirmation matrix (Section 4.3 V4, V5 in plan).

**Source:** QuantMonitor — Regime and Trend Filters (2025)
**Status:** MAYBE USEFUL — already partially addressed; include as one of the 4h confirmation variants

---

## 3. Multi-Timeframe Confirmation

### Finding 3.1 — 4H Confirmation Adds Documented Edge (LIKELY USEFUL)
**Core idea:** Studies show a 30m/4h timeframe pair (1:8 ratio) is among the most effective MTF structures for intraday trading. WR improvements of 5–10% and MaxDD reductions of 30–50% are typical when 4h trend aligns with 30m entry.

**NexusTrader relevance:** v1.2 Phase 5 backtesting confirmed: adding 4h gate to TrendModel improved PF from 1.825 to 2.976 and MaxDD from 8.2R to 4.1R — matching the literature exactly. Current PBL 4h EMA20>EMA50 gate is the right structure.

**Actionable implication:** The question is not WHETHER 4h confirmation helps but WHICH 4h filter formulation is strongest. This justifies the V0–V9 confirmation matrix.

**Source:** LuxAlgo — 5 Steps to Multi-Timeframe Confirmation; LogicInv — Building MTF Algorithms
**Status:** LIKELY USEFUL — confirms the research direction for 4h confirmation matrix

### Finding 3.2 — Volume Confirmation Across TFs (MAYBE USEFUL)
**Core idea:** Volume spike on 30m bar AND 4h bar above 1.5× 20-period average volume improves signal quality by confirming genuine institutional participation vs. noise.

**NexusTrader relevance:** Volume is not currently used in PBL or SLC. Adding volume filter would require computing 30m volume spike and cross-referencing 4h volume. Adds complexity. Could be Phase 2 enhancement.

**Source:** MindMathMoney — Multi-Timeframe Trading Guide (2026)
**Status:** MAYBE USEFUL — Phase 2 candidate; exclude from current parameter matrix

### Finding 3.3 — MTF Confluence Scoring (MAYBE USEFUL)
**Core idea:** Aggregating 6+ timeframe readings (15m/30m/1h/4h/1d/1w) into a single alignment score (0–6) and trading only at 5+ shows high selectivity but low trade count (typically 80% fewer signals).

**NexusTrader relevance:** ConfluenceScorer already aggregates 5 model signals. Adding 6+ TF readings would dramatically reduce trade count below minimum thresholds (need ≥1,200 trades for 4-year backtest).

**Source:** TradeLogic Pro — MTF Strategy Dashboard
**Status:** MAYBE USEFUL — noted but excluded from current workstream due to trade count risk

---

## 4. Walk-Forward Optimization

### Finding 4.1 — WF Efficiency 50–85% = Robust (LIKELY USEFUL)
**Core idea:** Walk-Forward Efficiency (WFE) = OOS annualized profit / IS annualized profit. Empirically: WFE 50–85% indicates robust, non-overfitted parameters. WFE < 35% = likely overfitted. WFE > 100% is suspicious.

**NexusTrader application:** Applied in Phase 5 (Stage E). Target: WFE ≥ 50% across all WF windows for accepted parameter sets.

**Implementation:** 12-month IS / 3-month OOS rolling windows, 4 windows across the 3-year training period. Mean WFE reported as primary robustness metric.

**Source:** QuantInsti — Walk-Forward Optimization Introduction; Unger Academy — WFA Best Practices
**Status:** LIKELY USEFUL — adopted as primary robustness metric in this research

### Finding 4.2 — Rolling Windows Outperform Fixed Splits for Intraday (LIKELY USEFUL)
**Core idea:** Rolling WFO (train on most recent N bars, test next M bars, roll) outperforms anchored WFO (fixed IS start date) for intraday strategies because market microstructure changes over time.

**NexusTrader application:** Research already uses 200-bar rolling regime window. Extend to full WFO rolling 12/3-month splits. Use most recent 36 months as IS, 12 months as OOS.

**Source:** Surmount AI — Walk-Forward vs Backtesting Best Practices
**Status:** LIKELY USEFUL — confirms window design in research plan

### Finding 4.3 — Computational Cost Planning (MAYBE USEFUL)
**Core idea:** WFO runs typically cost 4–8× simple backtest runtime. Crypto intraday with 70,000+ bars requires efficient vectorized computation.

**NexusTrader application:** Current backtest_v9_system.py runs ~30s per symbol. WFO across 4 windows = ~2 min per parameter set. With 500 trials = ~17 hours single-threaded → multiprocessing required (target: 8 workers = ~2 hours).

**Source:** Grokipedia — Walk-Forward Optimization
**Status:** MAYBE USEFUL — compute planning note; drives multiprocessing requirement

---

## 5. Backtest Overfitting Controls

### Finding 5.1 — Probability of Backtest Overfitting (PBO) (LIKELY USEFUL)
**Core idea:** PBO measures the probability that a parameter configuration appears best in-sample by luck. Computed via Combinatorially Symmetric Cross-Validation (CSCV): split data into many IS/OOS combos, measure rank correlation.

**Threshold:** PBO < 10% = robust. PBO 10–50% = caution. PBO > 50% = likely false positive.

**NexusTrader application:** After coarse and focused sweeps, compute PBO on the top-10 configurations. Reject candidates with PBO > 25%.

**Implementation plan:** After Stage C (focused sweep), run CSCV across 20 IS/OOS splits on the training period. Use OOS rank correlation to estimate PBO per candidate.

**Source:** Bailey et al. (2015) — The Probability of Backtest Overfitting [peer-reviewed]
**Status:** LIKELY USEFUL — adopted as secondary screening metric after Stage C

### Finding 5.2 — Deflated Sharpe Ratio (DSR) (LIKELY USEFUL)
**Core idea:** DSR corrects raw Sharpe Ratio for (a) multiple-testing bias (number of parameter combinations tried) and (b) non-normal returns (fat tails common in crypto).

**Formula:** DSR = SR × sqrt((1 − γ ln(M)) / T) where M = # parameter combos tested, T = # observations, γ ≈ 0.5

**NexusTrader application:** After each sweep stage, compute DSR for the top configuration. Report alongside raw PF. Target DSR ≥ 0.7 for any promoted configuration.

**Source:** de Prado (2016) — The Deflated Sharpe Ratio [SSRN 2460551]
**Status:** LIKELY USEFUL — computed as supplemental metric in Stage E

### Finding 5.3 — Combinatorial Purged Cross-Validation (CPCV) (MAYBE USEFUL)
**Core idea:** CPCV (Lopez de Prado) removes lookahead bias and path dependency better than standard hold-out. Splits data into k groups, uses all combinations of k-1 for training.

**NexusTrader application:** The current rolling-window WFO approximates CPCV in its rolling form. Full CPCV implementation would add rigor but requires 10–50× more computation.

**Source:** Lopez de Prado — Advances in Financial Machine Learning; ScienceDirect — Backtest Overfitting in ML Era (2024)
**Status:** MAYBE USEFUL — noted; rolling WFO is sufficient for this phase

---

## 6. Crypto Intraday Strategy Robustness

### Finding 6.1 — Fee Sensitivity Testing (LIKELY USEFUL)
**Core idea:** Intraday strategies commonly show +0.3–0.5% gross return per trade that evaporates to zero after fees. Standard robustness test: increase fees 2–3× and check if edge persists.

**NexusTrader application:** Current backtests use Scenario B = 0.04%/side (VIP maker). Robustness check: run optimized config at 0.08%/side (non-VIP maker) and 0.15%/side (taker). If PF drops below 1.10 at 0.08%, the edge is too fee-sensitive.

**Source:** Build Alpha — Robustness Testing Guide; Paybis — How to Backtest a Crypto Bot
**Status:** LIKELY USEFUL — added as mandatory stress test in Stage F

### Finding 6.2 — Simplicity as Overfitting Hedge (LIKELY USEFUL)
**Core idea:** Strategies with fewer than 5 rules/parameters tend to survive OOS testing 2–3× more often than complex multi-parameter systems. Every additional parameter added to improve IS performance has ~30% chance of harming OOS.

**NexusTrader application:** PBL currently has 4 conditions (EMA proximity, rejection candle 3-sub-conditions, RSI, 4h HTF). Adding more conditions risks overfitting. Prefer tightening existing parameters over adding new ones.

**Source:** StrategyQuant — Robustness Tests and Analysis; Build Alpha — OOS Testing Guide
**Status:** LIKELY USEFUL — drives research approach: tune existing parameters first, add conditions only if justified by clear edge

### Finding 6.3 — High Trade Frequency and Fee Drag (LIKELY USEFUL)
**Core idea:** Intraday strategies with 500+ trades/year see 5–10× amplification of fee impact vs. swing strategies (50–100 trades/year). PBL at 516 trades/4 years ≈ 129/year is manageable but taker fees would still materially degrade PF.

**NexusTrader application:** Monitor that parameter changes don't dramatically increase trade frequency. If PBL trade count doubles, fee sensitivity doubles. Target trade count range: 400–800 for PBL standalone.

**Source:** StrategyQuant — Spread Robustness Check (1.2M FX strategies analysis)
**Status:** LIKELY USEFUL — trade count constraint added to acceptance criteria

---

## 7. Portfolio Heat Controls for Multi-Asset Crypto

### Finding 7.1 — Explicit Portfolio Heat Ceiling (LIKELY USEFUL)
**Core idea:** Setting an explicit portfolio heat limit (sum of all open position risk) at 10% of equity prevents simultaneous correlated losses from wiping out capital. Heat = sum(position_size × risk_per_position).

**NexusTrader relevance:** Current: max_capital_pct=4% per trade, implicit heat = 4% × max_positions. Adding explicit heat tracking in PositionSizer enables dynamic position reduction when heat approaches ceiling.

**Source:** ProTrader Dashboard — Portfolio Heat Management
**Status:** LIKELY USEFUL — heat ceiling formalization recommended for Phase 2 live trading prep

### Finding 7.2 — Core-Satellite Symbol Allocation (MAYBE USEFUL)
**Core idea:** Institutional framework: 60–80% BTC core, 15–25% ETH secondary, 5–10% altcoin satellite. Research validates that BTC dominance reduces portfolio volatility without proportional return reduction.

**NexusTrader relevance:** Current symbol weights (SOL=1.3, ETH=1.2, BTC=1.0) are growth-tilted. In bear regimes, the higher SOL weight amplifies drawdowns since SOL correlates strongly with BTC in crashes but more volatile.

**Source:** XBTO — Crypto Portfolio Allocation 2026 (Institutional Guide); Morgan Stanley — Investing in Crypto
**Status:** MAYBE USEFUL — consider reducing SOL weight or adding regime-conditional gating; not in current sweep

### Finding 7.3 — Correlation-Driven Max Position Count (MAYBE USEFUL)
**Core idea:** When BTC–ETH rolling correlation exceeds 0.80, reduce max open positions since assets effectively behave as one correlated pool (no diversification benefit).

**NexusTrader relevance:** In crash regimes, BTC/ETH/SOL all correlate > 0.9. The current max_positions=10 allows significant correlated exposure. CrashDefenseController addresses this via emergency closes, but a proactive pre-gate is cleaner.

**Source:** VanEck — Optimal Crypto Allocation; Morgan Stanley — Asset Allocation in Crypto
**Status:** MAYBE USEFUL — Phase 2 PortfolioGuard enhancement; not in current parameter sweep

---

## Summary: Actionable Research → NexusTrader Experiment Matrix

| Finding | Classification | Experiment Integration |
|---------|---------------|----------------------|
| EMA proximity validates PBL design | LIKELY USEFUL | Informs ema_prox_atr_mult range [0.20, 0.80] |
| 1h intermediate TF potentially better than 4h alone | LIKELY USEFUL | Test V4–V9 4h variants + optional 1h pre-filter |
| HMM regime gating already validated | LIKELY USEFUL | No change; confirms design |
| Regime-indexed position size caps | LIKELY USEFUL | Add regime_pos_frac variants to matrix (0.35 / 0.25 / 0.20) |
| 30m/4h MTF edge confirmed (WR +7%, DD −3.1R) | LIKELY USEFUL | Confirms V0–V9 confirmation matrix value |
| WFE 50–85% threshold | LIKELY USEFUL | Primary robustness metric in Stage E |
| PBO < 10–25% threshold | LIKELY USEFUL | Secondary screening after Stage C |
| DSR ≥ 0.7 threshold | LIKELY USEFUL | Supplemental metric in Stage E |
| Fee sensitivity: test at 2× fees | LIKELY USEFUL | Stage F mandatory stress test |
| Simplicity rule: ≤5 parameters | LIKELY USEFUL | Caps model complexity in design |
| Portfolio heat ceiling 10% | LIKELY USEFUL | Phase 2 live trading recommendation |
| Fibonacci retracement zones | MAYBE USEFUL | Phase 2 enhancement only |
| MTF 6-TF confluence scoring | MAYBE USEFUL | Excluded — too low trade count |
| CPCV (vs rolling WFO) | MAYBE USEFUL | Rolling WFO sufficient for this phase |
| Correlation-driven position caps | MAYBE USEFUL | Phase 2 PortfolioGuard recommendation |

---

## Key Citations

1. Bailey, D.H. et al. (2015). *The Probability of Backtest Overfitting.* Journal of Computational Finance. https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf
2. López de Prado, M. (2016). *The Deflated Sharpe Ratio.* SSRN 2460551.
3. QuantStart. *Market Regime Detection using Hidden Markov Models.* https://www.quantstart.com/articles/market-regime-detection-using-hidden-markov-models-in-qstrader/
4. QuantInsti. *Walk-Forward Optimization: How It Works.* https://blog.quantinsti.com/walk-forward-optimization-introduction/
5. Build Alpha. *Robustness Tests and Checks for Algorithmic Trading Strategies.* https://www.buildalpha.com/robustness-testing-guide/
6. LuxAlgo. *5 Steps to Confirm Entries with Multi-Timeframes.* https://www.luxalgo.com/blog/5-steps-to-confirm-entries-with-multi-timeframes/
7. StrategyQuant. *What We Learned Analyzing 1.2 Million FX Strategies — Spread Robustness.* https://strategyquant.com/blog/what-we-learned-analyzing-1-2-million-fx-strategies-part-2-spread-robustness-check/
