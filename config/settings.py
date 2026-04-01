# ============================================================
# NEXUS TRADER — Application Settings Manager
# ============================================================

import yaml
import logging
from pathlib import Path
from typing import Any, Optional
from config.constants import CONFIG_PATH

logger = logging.getLogger(__name__)

# Default configuration values
DEFAULT_CONFIG = {
    "disabled_models": [],  # model names to skip in SignalGenerator (e.g. ["mean_reversion", "liquidity_sweep"])
    "model_weights": {
        "trend": 0.35,
        "mean_reversion": 0.25,
        "momentum_breakout": 0.25,
        "donchian_breakout": 0.25,   # Session 48 — same weight as momentum_breakout (research)
        "vwap_reversion": 0.28,
        "liquidity_sweep": 0.15,
        "funding_rate": 0.20,
        "order_book": 0.18,
        "sentiment": 0.12,
        "rl_ensemble": 0.0,
        "orchestrator": 0.22,
    },
    "models": {
        "trend": {
            "entry_buffer_atr": 0.20,
            "adx_min": 25.0,
            "rsi_long_min": 45,
            "rsi_long_max": 70,
            "rsi_short_min": 30,
            "rsi_short_max": 55,
            "strength_base": 0.15,
            "ema20_bonus": 0.15,
            "macd_bonus": 0.20,
            "adx_bonus_max": 0.30,
        },
        "momentum_breakout": {
            "entry_buffer_atr": 0.10,
            "lookback": 20,
            "vol_mult_min": 1.5,
            "rsi_bullish": 55,
            "rsi_bearish": 45,
            "strength_base": 0.35,
        },
        "donchian_breakout": {
            "entry_buffer_atr": 0.10,
            "lookback": 20,
            "vol_mult_min": 1.3,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 3.0,
            "rsi_long_min": 50.0,
            "rsi_short_max": 50.0,
            "strength_base": 0.35,
        },
        "vwap_reversion": {
            "entry_buffer_atr": -0.10,
            "z_threshold": 1.5,
            "rsi_oversold": 42,
            "rsi_overbought": 58,
            "deviation_window": 20,
            "sl_atr_mult": 1.2,
            "tp_atr_offset": 0.5,
        },
        "mean_reversion": {
            "entry_buffer_atr": -0.15,
            "bb_lower_dist": 0.15,
            "rsi_oversold": 35,
            "rsi_overbought": 65,
            "stoch_rsi_oversold": 25,
            "stoch_rsi_overbought": 75,
        },
        "liquidity_sweep": {
            "swing_lookback": 15,
            "min_sweep_pct": 0.10,
            "vol_mult_min": 1.3,
            "cascade_risk_cutoff": 0.70,
            "liq_density_threshold": 0.60,
        },
        "funding_rate": {
            "min_signal": 0.40,
            "min_confidence": 0.55,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
        },
        "order_book": {
            "min_signal": 0.35,
            "min_confidence": 0.60,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.0,
            "max_timeframe": "30m",
        },
        "sentiment": {
            "min_signal": 0.35,
            "min_confidence": 0.55,
            "min_headlines": 3,
            "max_age_minutes": 90,
            "sl_atr_mult": 1.5,
            "tp_atr_mult": 2.5,
        },
    },
    "regime_affinity": {
        "trend": {"bull_trend": 1.0, "bear_trend": 0.9, "ranging": 0.1, "volatility_expansion": 0.25, "volatility_compression": 0.2, "uncertain": 0.3, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.3, "recovery": 0.7, "accumulation": 0.2, "distribution": 0.2},
        "mean_reversion": {"bull_trend": 0.05, "bear_trend": 0.08, "ranging": 1.0, "volatility_expansion": 0.02, "volatility_compression": 0.8, "uncertain": 0.20, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.4, "recovery": 0.4, "accumulation": 0.8, "distribution": 0.7},
        "momentum_breakout": {"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.1, "volatility_expansion": 0.70, "volatility_compression": 0.1, "uncertain": 0.2, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.8, "recovery": 0.6, "accumulation": 0.3, "distribution": 0.4},
        "donchian_breakout": {"bull_trend": 0.85, "bear_trend": 0.85, "ranging": 0.15, "volatility_expansion": 0.90, "volatility_compression": 0.10, "uncertain": 0.25, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.75, "recovery": 0.65, "accumulation": 0.35, "distribution": 0.50},
        "vwap_reversion": {"bull_trend": 0.5, "bear_trend": 0.5, "ranging": 0.8, "volatility_expansion": 0.15, "volatility_compression": 0.7, "uncertain": 0.5, "crisis": 0.1, "liquidation_cascade": 0.1, "squeeze": 0.4, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.6},
        "liquidity_sweep": {"bull_trend": 0.4, "bear_trend": 0.6, "ranging": 0.9, "volatility_expansion": 0.25, "volatility_compression": 0.5, "uncertain": 0.4, "crisis": 0.2, "liquidation_cascade": 0.3, "squeeze": 0.5, "recovery": 0.5, "accumulation": 0.7, "distribution": 0.8},
        "funding_rate": {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.5, "volatility_expansion": 0.7, "volatility_compression": 0.4, "uncertain": 0.5, "crisis": 0.6, "liquidation_cascade": 0.7, "squeeze": 0.8, "recovery": 0.7, "accumulation": 0.5, "distribution": 0.6},
        "order_book": {"bull_trend": 0.7, "bear_trend": 0.7, "ranging": 0.6, "volatility_expansion": 0.5, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.3, "liquidation_cascade": 0.2, "squeeze": 0.6, "recovery": 0.6, "accumulation": 0.6, "distribution": 0.7},
        "sentiment": {"bull_trend": 0.9, "bear_trend": 0.7, "ranging": 0.4, "volatility_expansion": 0.5, "volatility_compression": 0.3, "uncertain": 0.4, "crisis": 0.2, "liquidation_cascade": 0.1, "squeeze": 0.4, "recovery": 0.8, "accumulation": 0.7, "distribution": 0.3},
        "rl_ensemble": {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.7, "volatility_expansion": 0.6, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.0, "liquidation_cascade": 0.0, "squeeze": 0.5, "recovery": 0.7, "accumulation": 0.7, "distribution": 0.6},
        "orchestrator": {"bull_trend": 0.8, "bear_trend": 0.8, "ranging": 0.7, "volatility_expansion": 0.7, "volatility_compression": 0.6, "uncertain": 0.5, "crisis": 0.1, "liquidation_cascade": 0.1, "squeeze": 0.6, "recovery": 0.7, "accumulation": 0.7, "distribution": 0.7},
    },
    "app": {
        "theme": "dark",
        "language": "en",
        "auto_start_feeds": False,
    },
    "risk": {
        "max_position_pct": 2.0,
        "max_portfolio_drawdown_pct": 15.0,
        "max_strategy_drawdown_pct": 10.0,
        "min_sharpe_live": 0.5,
        "max_spread_pct": 0.3,
        "max_open_positions": 10,
        "default_stop_loss_pct": 2.0,
        "default_take_profit_pct": 4.0,
        # IDSS RiskGate parameters (hot-applied to scanner on save)
        "max_concurrent_positions": 10,   # Updated to match config.yaml runtime value
        "min_risk_reward": 1.3,
    },
    "idss": {
        # Minimum confluence score to generate an OrderCandidate (hot-applied on save)
        "min_confluence_score": 0.55,
    },
    "ai": {
        "openai_model": "gpt-4o",
        "anthropic_model": "claude-opus-4-6",
        "strategy_generation_enabled": True,
        "ml_confidence_threshold": 0.65,
        "retrain_interval_hours": 24,
    },
    "sentiment": {
        "news_enabled": True,
        "reddit_enabled": False,
        "twitter_enabled": False,
        "onchain_enabled": False,
        "update_interval_minutes": 15,
    },
    "backtesting": {
        "default_fee_pct": 0.1,
        "default_slippage_pct": 0.05,
        "default_initial_capital": 10000.0,
        "walk_forward_train_months": 24,
        "walk_forward_validate_months": 6,
        "walk_forward_step_months": 3,
    },
    "data": {
        "default_timeframe": "1h",
        "historical_days": 365,
        "max_candles_per_request": 1000,
        "cache_enabled": True,
        "websocket_enabled": True,
        "feed_interval_seconds": 3,
        "ws_reconnect_attempts": 5,
    },
    "notifications": {
        "desktop_enabled": True,
        "sound_enabled": False,
        "trade_alerts": True,
        "strategy_alerts": True,
        "system_alerts": True,
        "dedup_window_seconds": 60,
        # Per-channel configs (secrets via vault, not stored here)
        "whatsapp": {
            "enabled": False,
            "from_number": "",
            "to_number": "",
        },
        "telegram": {
            "enabled": False,
            "chat_id": "",
        },
        "email": {
            "enabled": False,
            "smtp_host": "smtp.gmail.com",
            "smtp_port": 587,
            "username": "",
            "from_address": "",
            "to_addresses": "",
            "use_tls": True,
        },
        "sms": {
            "enabled": False,
            "from_number": "",
            "to_number": "",
        },
        # Per-type enable/disable preferences
        "preferences": {
            "trade_opened":     True,
            "trade_closed":     True,
            "trade_stopped":    True,
            "trade_rejected":   False,
            "trade_modified":   False,
            "strategy_signal":  False,
            "risk_warning":     True,
            "market_condition": False,
            "system_error":     True,
            "emergency_stop":   True,
            "daily_summary":    True,
            "health_check":     True,
            "health_check_interval_hours": 6,   # valid: 1, 2, 3, 4, 6, 12, 24
        },
    },
    "agents": {
        "auto_start": True,
        "min_confluence_boost": 0.25,
        "funding_enabled": True,
        "orderbook_enabled": True,
        "options_enabled": True,
        "options_max_days_expiry": 35,
        "onchain": {"enabled": True, "symbols": ["bitcoin", "ethereum"]},
        "volatility_surface": {"enabled": True},
        "liquidation_flow": {"enabled": True},
        # API keys stored in vault — placeholders here for discovery
        "fred_api_key": "__vault__",
        "lunarcrush_api_key": "__vault__",
        "coinglass_api_key": "",
        "cryptopanic_api_key": "",
    },
    "execution": {
        "base_size_usdt": 500.0,
        "auto_execute_enabled": False,
        "auto_execute_min_confidence": 0.72,
        "auto_execute_min_signal": 0.55,
        "auto_execute_regime_whitelist": ["TRENDING_UP", "TRENDING_DOWN", "RECOVERY"],
    },
    # Phase 1 — Trade filters
    "filters": {
        "time_of_day": {
            "enabled": False,   # hypothesis filter — not yet validated; prod config overrides
            "start_hour_utc": 12,
            "end_hour_utc": 21,
        },
        "volatility": {
            "enabled": True,
            "min_atr_ratio": 0.5,
        },
        "model_auto_disable": {
            "enabled": False,
            "min_trades": 20,              # absolute minimum before any criterion fires
            "wr_threshold": 0.40,          # rolling WR below this is criterion 1
            # v2 additional criteria (all must fail before global disable):
            "expectancy_threshold": -0.10, # avg R < this = negative expected value
            "pf_threshold": 0.85,          # profit factor below this after 30+ trades
        },
        "btc_trend": {
            "enabled": False,
            "strong_trend_margin_pct": 0.5,
        },
    },
    # Phase 1 settings
    "scanner": {
        "btc_only_mode": False,
        "websocket_enabled": False,
        "websocket_symbol": "BTC/USDT",
        "websocket_timeframe": "1h",
        "ohlcv_bars": 300,
        # Auto-execute: when True, approved IDSS candidates are submitted to
        # PaperExecutor automatically after each scan cycle (no manual click needed).
        # Default is True — NexusTrader should auto-execute on every restart.
        "auto_execute": True,
        "auto_execute_cooldown_seconds": 30,
    },
    # Phase 2 settings
    "regime": {
        "use_ensemble": True,
        "hmm_weight": 0.35,
        "rule_weight": 0.65,
    },
    # Phase 3 settings
    "rl": {
        "enabled": False,
        "model_dir": "",
        "replay_buffer_size": 50000,
        "reward_leverage": 10.0,
        "train_every_n_candles": 10,
        "shadow_only": True,
    },
    "orchestrator": {
        "veto_enabled": True,
    },
    # Phase 4 settings — OI signal
    "oi_signal": {
        "enabled": True,
        # Independent ablation toggles — disable individually to measure contribution:
        #   oi_modifier_enabled=false  → removes trend-confirm/weak-trend/spike logic
        #   liq_modifier_enabled=false → removes liquidation cluster bonus
        "oi_modifier_enabled": True,
        "liq_modifier_enabled": True,
        "spike_threshold_pct": 30.0,
        "trend_confirm_bonus": 0.05,
        "weak_trend_penalty": 0.03,
        # liq_clusters_enabled retained for backward-compat (superseded by liq_modifier_enabled)
        "liq_clusters_enabled": True,
        "liq_density_threshold": 0.70,
        "liq_cluster_bonus": 0.04,
        # Session 23: data quality gate — suppress modifier when data quality < this score
        # 0=no_agent, 1=no_data, 2=stale/spike, 3=fresh_normal. Default 2 = require fresh.
        "min_data_quality": 2,
    },
    # Session 23: Correlation dampening — reduces double-counting of correlated signals
    "correlation_dampening": {
        "enabled": True,
        # Global minimum factor (floor) — correlated models never get less than this weight
        "min_factor": 0.50,
    },
    # Session 23: Portfolio correlation guard
    "portfolio_guard": {
        "enabled": True,
        # Hard block when this many same-direction correlated positions are already open
        "max_same_group_same_dir": 4,
        # Size multipliers for N=0,1,2,3,4 same-group same-direction positions
        "multipliers": [1.00, 0.80, 0.55, 0.30, 0.10],
    },
    # Session 24: Symbol Priority & Allocation System
    # Weights are ranking-only — they never affect signals, sizing, or risk.
    # adjusted_score = base_score × symbol_weight; candidates ranked by adjusted_score.
    "symbol_allocation": {
        # STATIC — fixed weights per symbol
        # DYNAMIC — weights switch between three profiles based on BTC dominance
        "mode": "STATIC",
        # ── Static weights (Study 4 baseline) ────────────────────────────────
        "static_weights": {
            "BTC/USDT": 1.0,
            "ETH/USDT": 1.2,
            "SOL/USDT": 1.3,
            "BNB/USDT": 0.8,
            "XRP/USDT": 0.8,
        },
        # ── BTC Dominance (DYNAMIC mode) ─────────────────────────────────────
        # User updates btc_dominance_pct manually or via future agent integration.
        "btc_dominance_pct":  50.0,    # current BTC dominance reading
        "btc_dominance_high": 55.0,    # above → BTC_DOMINANT
        "btc_dominance_low":  45.0,    # below → ALT_SEASON (between → NEUTRAL)
        # ── Regime profiles (DYNAMIC mode) ───────────────────────────────────
        "profiles": {
            # BTC dominance > high_threshold: favour BTC/ETH over alts
            "btc_dominant": {
                "BTC/USDT": 1.4,
                "ETH/USDT": 1.1,
                "SOL/USDT": 0.9,
                "BNB/USDT": 0.7,
                "XRP/USDT": 0.7,
            },
            # btc_dominance_low ≤ dominance ≤ btc_dominance_high: balanced
            "neutral": {
                "BTC/USDT": 1.0,
                "ETH/USDT": 1.2,
                "SOL/USDT": 1.3,
                "BNB/USDT": 0.8,
                "XRP/USDT": 0.8,
            },
            # BTC dominance < low_threshold: alt season, favour alts
            "alt_season": {
                "BTC/USDT": 0.7,
                "ETH/USDT": 1.2,
                "SOL/USDT": 1.5,
                "BNB/USDT": 1.0,
                "XRP/USDT": 1.0,
            },
        },
    },
    # ── Performance Framework (Session 27) ───────────────────────────────────
    # RAG threshold system — controls when verdicts are issued.
    # Min trades before any non-INSUFFICIENT_DATA verdict.
    "performance_thresholds": {
        "min_trades_for_verdict":    20,    # trades below this → INSUFFICIENT_DATA
        # Hard performance block thresholds (applied in paper_executor.submit)
        "hard_block_pf_below":       1.0,   # portfolio PF must be below this
        "hard_block_wr_below":       0.40,  # portfolio WR must be below this
        "hard_block_min_trades":     30,    # minimum trades before hard block can fire
    },
    # Scale manager — phase definitions (informational, actual phases defined in code)
    "scale_manager": {
        "current_phase":             1,     # updated by operator after manual advancement
        "phase1_risk_pct":           0.005, # 0.5%
        "phase2_risk_pct":           0.0075,# 0.75%
        "phase3_risk_pct":           0.010, # 1.0%
        "phase1_min_trades":         50,    # trades in phase 1 before advancement possible
        "phase2_min_trades":         50,    # trades in phase 2 before advancement possible
    },
    # Phase 3 settings — Probability calibrator
    "probability_calibrator": {
        "enabled": True,
    },
    # Phase 4 settings
    "multi_asset": {
        "enabled": False,
    },
    "finbert": {
        "enabled": True,
        "min_confidence": 0.55,
        "min_headlines": 3,
        "min_net_score": 0.35,
    },
    "ms_garch": {
        "enabled": True,
        "refit_every_n_bars": 100,
    },
    "rlmf": {
        "enabled": False,
        "feedback_interval_episodes": 50,
    },
    "hmm_regime": {
        "enabled": True,
        "n_components": 6,
        "min_train_bars": 300,
        "retrain_every_n_bars": 50,
        "hmm_rule_blend_weight": 0.60,
    },
    "crash_detector": {
        "enabled": True,
        "eval_interval_seconds": 60,
        "recovery_bars_required": 5,
        "recovery_hysteresis": 1.5,
        "weights": {
            "atr_spike": 2.0,
            "price_velocity": 1.8,
            "liquidation_cascade": 1.5,
            "cross_asset_decline": 1.5,
            "orderbook_imbalance": 1.2,
            "funding_rate_flip": 1.0,
            "oi_collapse": 1.0,
        },
        "tier_thresholds": {
            "defensive": 5.0,
            "high_alert": 7.0,
            "emergency": 8.0,
            "systemic": 9.0,
        },
    },
    "adaptive_activation": {
        "enabled": True,
        "min_activation_weight": 0.10,
        "crash_long_multiplier": 0.0,
        "crash_short_multiplier": 1.5,
    },
    "dynamic_confluence": {
        "enabled": False,   # PRODUCTION: fixed threshold 0.20 per Study 4 backtest
        "base_threshold": 0.20,
        "min_floor": 0.20,
        "max_ceiling": 0.20,
        "regime_confidence_high": 0.70,
        "regime_confidence_low": 0.40,
        "vol_expansion_factor": 1.15,
        "vol_compression_factor": 0.95,
        "win_rate_tracking": True,
        "win_rate_window": 30,
    },
    "expected_value": {
        "enabled": True,
        "ev_threshold": 0.05,
        "min_rr_floor": 1.0,
        # Lowered from 0.55 → 0.50 to eliminate the dead zone where signals above
        # min_confluence_score (0.45) always produce negative EV.  At midpoint=0.50
        # a score of 0.45 yields win_prob≈0.40; a score of 0.50 yields win_prob=0.50.
        # Positive EV is achievable at R:R ≥ 1.5 for scores ≥ 0.50, consistent with
        # the dynamic confluence threshold range (0.28–0.65).
        "score_midpoint": 0.50,
        "sigmoid_steepness": 8.0,
        "regime_uncertainty_penalty": 0.15,
    },
    "risk_engine": {
        # ── Production sizing config (Study 4 Moderate scenario) ──
        "sizing_mode": "risk_based",       # "risk_based" | "kelly"
        "risk_pct_per_trade": 0.5,         # % of capital to risk per trade — Phase 1 (0.5%)
        # ── Portfolio heat ──────────────────────────────────────────
        "portfolio_heat_max_pct": 0.04,   # 4% max portfolio heat — Study 4 validated
        "vol_adjusted_sizing": True,
        "atr_percentile_lookback_days": 90,
        "loss_streak_trigger": 3,
        "loss_streak_size_multiplier": 0.50,
        "fat_tail_risk_multiplier": 1.5,
        "kelly_fraction": 0.25,
        "max_position_pct": 0.04,
        "min_position_pct": 0.003,
        "max_leverage_by_regime": {
            "bull_trend": 3.0,
            "bear_trend": 2.0,
            "ranging": 1.5,
            "volatility_expansion": 1.5,
            "uncertain": 1.0,
        },
        "max_leverage_defensive_mode": 1.0,
        "max_positions_per_symbol": 10,
        "max_trades_per_scan_cycle": 1,
        # ── Daily loss limit kill switch ────────────────────────────
        # Blocks new entries when today's realized P&L <= -(limit_pct)% of initial capital.
        # Set to 0.0 to disable. Resets at UTC midnight. Phase 1 default: 2.0%.
        "daily_loss_limit_pct": 2.0,
    },
    "staged_candidates": {
        "enabled": True,
        "ttl_seconds": 10800,           # 3 hours — max age before expiry
        "max_active": 20,               # capacity limit
        "retention_seconds": 86400,     # keep terminal candidates 24h for audit
    },
    "multi_tf": {
        # Enabled for demo trading: signals that directly contradict the
        # higher-timeframe regime (e.g., 1h buy vs 4h bear_trend) are rejected.
        "confirmation_required": True,
        "confirmation_timeframes": {
            "1h": "4h",
            "4h": "1d",
            "1d": "1w",
        },
    },
    # Directional conflict threshold for ConfluenceScorer.
    # If abs(long_weight - short_weight) / total < min_direction_dominance,
    # the candidate is rejected as too conflicted.
    "confluence": {
        "min_direction_dominance": 0.30,
    },
    # Per-symbol HMM persistence configuration.
    "hmm_per_symbol": {
        "enabled": True,                # use per-symbol HMM instances
        "retrain_every_n_scans": 50,    # retrain a symbol's HMM every N scan cycles
    },
    # Entry price model configuration.
    "entry_model": {
        "enabled": True,               # apply ENTRY_BUFFER_ATR offsets
    },
    # LTF (Lower-Timeframe) confirmation for staged candidates.
    # ── BACKTEST PARITY MODE (Session 51) ────────────────────────────────────
    # When enabled, demo execution uses IDENTICAL sizing, exit logic, and
    # constraints as BacktestRunner._run_scenario().  AI agents remain active
    # as a FILTER-ONLY layer (may block trades, never alter trade structure).
    #
    # Purpose: ensure demo produces the same trades as backtest, minus any
    # trades blocked by the AI confluence/orchestrator layer.
    "execution_mode": {
        "backtest_parity": False,     # master gate for parity mode
        # Parity constants (must match backtest_runner.py exactly):
        "parity_pos_frac":        0.35,
        "parity_max_heat":        0.80,
        "parity_max_positions":   10,
        "parity_max_per_asset":   3,
        "parity_initial_capital": 100_000.0,
        # When True, AI agents can only FILTER (block) trades, never modify
        # position size, SL, TP, or any trade parameter after signal generation.
        "ai_filter_only":         True,
    },
    # PBL + SLC model parameter overrides (Session 50 — PBL optimization)
    "mr_pbl_slc": {
        "pullback_long": {
            "ema_prox_atr_mult":  0.5,    # |close - EMA50| ≤ mult × ATR14
            "sl_atr_mult":        2.5,    # stop-loss = close − mult × ATR14
            "tp_atr_mult":        3.0,    # take-profit = close + mult × ATR14
            "rsi_min":            40.0,   # RSI14 must be above this
            "wick_strength":      1.0,    # lower_wick ≥ mult × body  (1.0 = original lw>body)
        },
    },
    # Phase 2c — Enhancement Layer + RangeBreakout + Shadow Tracking
    "phase_2c": {
        "pullback_enhancement": {
            "enabled": False,
            "boost_flat": 0.10,
            "boost_confidence_scale": 0.30,
            "mode_b_level": 0,
            "relaxed_strength_cap": 0.75,
        },
        "range_breakout": {
            "enabled": False,
            "entry_buffer_atr": 0.1,
            "sl_range_pct": 0.10,
            "tp_range_mult": 1.0,
            "min_confidence": 0.35,
            "strength_base": 0.35,
            "strength_cap": 0.80,
            "max_positions": 1,
            "max_capital_pct": 0.03,
        },
        "shadow_tracking": {
            "enabled": True,
        },
    },
    # 15m closed-candle confirmation before executing HTF signals.
    "ltf_confirmation": {
        "ema_period": 9,               # EMA span for trend alignment
        "rsi_period": 14,              # RSI lookback
        "rsi_max_long": 72.0,          # reject long if 15m RSI above this
        "rsi_min_short": 28.0,         # reject short if 15m RSI below this
        "rsi_void_long": 78.0,         # void long candidate (anti-churn)
        "rsi_void_short": 22.0,        # void short candidate (anti-churn)
        "volume_ratio_min": 0.6,       # min volume vs 20-bar average (lowered from 0.80 — backtest validated)
        "volume_lookback": 20,         # bars for volume average
        "ema_slope_bars": 3,           # bars for EMA trend direction
        "timeframe": "15m",            # LTF candle timeframe
        "ohlcv_limit": 100,            # bars to fetch for LTF evaluation
    },
}


class AppSettings:
    """Manages application configuration with YAML file persistence."""

    def __init__(self):
        self._config: dict = {}
        self.load()

    def load(self):
        """Load config from YAML file, merging with defaults."""
        self._config = self._deep_merge(DEFAULT_CONFIG.copy(), {})
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r") as f:
                    file_config = yaml.safe_load(f) or {}
                self._config = self._deep_merge(self._config, file_config)
                logger.debug("Configuration loaded from %s", CONFIG_PATH)
            except Exception as e:
                logger.warning("Could not load config file: %s — using defaults", e)
        else:
            self.save()

        # D3: migrate any plain-text API keys left in YAML to the encrypted vault
        try:
            from core.security.key_vault import key_vault
            key_vault.migrate_from_settings()
        except Exception as exc:
            logger.debug("Vault migration skipped: %s", exc)

    def save(self):
        """Persist current config to YAML file.

        Uses an atomic write (temp file → fsync → rename) so that any write
        interruption or OS-level buffer flush boundary can never leave the live
        config.yaml in a truncated state.  The rename() call on POSIX is
        atomic with respect to the filesystem, so readers always see either the
        old complete file or the new complete file — never a partial write.

        WORKER-PROCESS GUARD: save() is a no-op in any process that is not the
        main application process (e.g. ProcessPoolExecutor backtest workers).
        Worker processes must never write config.yaml — only the main process
        owns the file.  Concurrent saves from 30+ worker processes caused the
        config.yaml corruption bug (Session 50 post-restart: WinError 5/32 storm
        → truncated YAML at line 587).
        """
        import multiprocessing
        if multiprocessing.current_process().name != "MainProcess":
            logger.debug(
                "settings.save(): skipped — worker process '%s' must not write config.yaml",
                multiprocessing.current_process().name,
            )
            return
        try:
            import os
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            yaml_bytes = yaml.dump(
                self._config, default_flow_style=False, indent=2,
            ).encode("utf-8")
            tmp_path = CONFIG_PATH.with_suffix(".yaml.tmp")
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            try:
                os.write(fd, yaml_bytes)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(str(tmp_path), str(CONFIG_PATH))
        except Exception as e:
            logger.error("Could not save config: %s", e)

    def get(self, key_path: str, default: Any = None) -> Any:
        """Get a config value using dot notation (e.g., 'risk.max_position_pct')."""
        keys = key_path.split(".")
        val = self._config
        for k in keys:
            if isinstance(val, dict) and k in val:
                val = val[k]
            else:
                return default
        return val

    # ── Demo-mode locked keys (cannot be mutated at runtime when demo_mode.locked=True) ──
    _DEMO_LOCKED_PREFIXES: tuple = (
        "mr_pbl_slc.pullback_long.",
        "disabled_models",
        "demo_mode.",
    )

    def _is_demo_locked(self, key_path: str) -> bool:
        """Return True if demo_mode is locked AND the key is in the immutable set."""
        if not self._config.get("demo_mode", {}).get("locked", False):
            return False
        return any(key_path.startswith(p) for p in self._DEMO_LOCKED_PREFIXES)

    def set(self, key_path: str, value: Any, auto_save: bool = True):
        """Set a config value using dot notation or top-level key, and persist.
        Examples:
            settings.set("risk.max_position_pct", 0.25)
            settings.set("risk", {"max_concurrent_positions": 3, ...})
            settings.set("key", val, auto_save=False)  # batch multiple sets,
                                                        # call save() manually after

        DEMO MODE LOCK: When demo_mode.locked=True, any attempt to mutate
        PBL params, disabled_models, or demo_mode keys is blocked and logged.
        """
        # ── Demo-mode immutability guard ─────────────────────────────────
        if self._is_demo_locked(key_path):
            logger.error(
                "settings.set(%r): BLOCKED — demo_mode is locked. "
                "PBL params and disabled_models are immutable during demo phase. "
                "To override, set demo_mode.locked=False in config.yaml manually.",
                key_path,
            )
            return
        if isinstance(key_path, str) and "." not in key_path and isinstance(value, dict):
            # Setting entire section
            self._config[key_path] = value
        else:
            # Setting nested key with dot notation
            keys = key_path.split(".")
            d = self._config
            for k in keys[:-1]:
                # Guard: if the current traversal node is not a dict (e.g. a string
                # was stored where a dict is expected), replace it with an empty dict
                # so the navigation can continue.  This prevents the
                # 'str' object does not support item assignment error.
                existing = d.get(k) if isinstance(d, dict) else None
                if not isinstance(existing, dict):
                    if not isinstance(d, dict):
                        logger.error(
                            "settings.set(%r): cannot navigate through non-dict "
                            "node at key %r (type=%s) — aborting set",
                            key_path, k, type(d).__name__,
                        )
                        return
                    logger.warning(
                        "settings.set(%r): intermediate key %r held %s=%r "
                        "instead of dict — replacing with empty dict",
                        key_path, k, type(existing).__name__, existing,
                    )
                    d[k] = {}
                d = d[k]
            d[keys[-1]] = value
        if auto_save:
            self.save()

    def get_section(self, section: str) -> dict:
        """Return an entire config section as a dict."""
        return self._config.get(section, {})

    def _deep_merge(self, base: dict, override: dict) -> dict:
        result = base.copy()
        for k, v in override.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                result[k] = self._deep_merge(result[k], v)
            else:
                result[k] = v
        return result


# Global singleton
settings = AppSettings()
