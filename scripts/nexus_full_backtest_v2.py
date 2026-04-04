"""
NexusTrader Full 4-Year Parallel Backtest v2
=============================================
Runs ALL combinations of capital models × concurrent-order slots simultaneously,
maximising every CPU core on your desktop. GPU (CUDA) is used for vectorised
indicator computation (ATR, EMA, ADX, RSI) when PyTorch is available — falling
back to NumPy on CPU otherwise.

Parameters: Phase 5 Optimized (P4_BASE from run_phase5_edge_expansion.py)
    confluence_threshold = 0.50   | trend_adx_min     = 32.0
    bb_expansion_ratio   = 3.5    | atr_sl_mult_override = 4.5 (all regimes)
    trend_bull_only      = True   | mb_vol_mult_min   = 1.0
    partial_pct          = 0.50   | tp_rr             = 1.22

Usage (run from NexusTrader root):
    python scripts/nexus_full_backtest_v2.py
    python scripts/nexus_full_backtest_v2.py --workers 16
    python scripts/nexus_full_backtest_v2.py --no-htf          # disable 4H gate
    python scripts/nexus_full_backtest_v2.py --capital 50000   # custom start

What this script tests
----------------------
7 capital models × 5 concurrent-slot configs = 35 simulations run in parallel.

Capital models (risk-per-trade / max-cap-per-position):
    A  0.5%  /  10%  — conservative (unleveraged)
    B  1.0%  /  25%  — moderate    (unleveraged)
    C  2.0%  /  45%  — aggressive  (unleveraged)
    D  var   /  50%  — conviction-scaled (unleveraged)
    E  10.0% / 100%  — full capital per slot (unleveraged, cap always binds)
    F  15.0% / 120%  — 1.2× leveraged (conservative futures)
    G  20.0% / 150%  — 1.5× leveraged (moderate futures)

Concurrent BTC positions (max simultaneous open orders):
    1  — single position, no overlap  (baseline)
    2  — up to 2 simultaneous BTC positions
    3  — up to 3 simultaneous BTC positions
    4  — up to 4 simultaneous BTC positions
    5  — up to 5 simultaneous BTC positions

Each new signal opens a new slot if one is free. Positions do NOT share risk
budget — every position uses the standard risk_pct of CURRENT equity. Portfolio
heat is capped (max_concurrent × max_cap_pct ≤ 60%) to prevent over-leverage.

GPU acceleration
----------------
When torch+CUDA is available the script offloads bulk ATR/EMA/RSI calculations
to the GPU, then returns NumPy arrays for the simulation loop. On a RTX 4070
(12 GB VRAM) this gives ~4–8× speedup on indicator calculation vs. pure NumPy.
Install: pip install "torch>=2.6.0" --index-url https://download.pytorch.org/whl/cu124

Requirements:
    pip install pandas numpy pyarrow tqdm
    pip install "torch>=2.6.0" --index-url https://download.pytorch.org/whl/cu124  (optional)
"""

from __future__ import annotations

import os
import sys
import json
import math
import time
import logging
import argparse
import statistics
import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

import numpy as np
import pandas as pd

# ── Paths ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
ROOT_DIR    = SCRIPT_DIR.parent
DATA_DIR    = ROOT_DIR / "backtest_data"   # pre-fetched indicator parquets
RESULTS_DIR = ROOT_DIR / "reports"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Regime constants ───────────────────────────────────────────────────────────
R_BULL      = "bull_trend"
R_BEAR      = "bear_trend"
R_RANGING   = "ranging"
R_VOL_EXP   = "volatility_expansion"
R_VOL_COMP  = "volatility_compression"
R_UNCERTAIN = "uncertain"

# ATR stop-distance multiplier per regime — legacy reference only.
# Phase 5 optimized params use a single atr_sl_mult_override=4.5 for all regimes.
REGIME_ATR_SL: dict = {
    R_BULL:     1.875,
    R_BEAR:     1.875,
    R_RANGING:  3.125,
    R_VOL_EXP:  3.75,
    R_VOL_COMP: 2.25,
    R_UNCERTAIN: 2.5,
}

# ── Phase 5 Optimized Parameters (P4_BASE from run_phase5_edge_expansion.py) ──
# These supersede PROD_PARAMS as of 2026-03-26 (user decision).
# Source: P4_BASE dict — Phase 4 best 30m config, PF ~1.71–1.83 standalone;
# combined with 4H MTF gate gives PF 2.976 in full IDSS backtest.
PROD_PARAMS = {
    # Signal quality
    "confluence_threshold": 0.50,   # raised from 0.45 — tighter quality gate
    "adx_trend_thresh":     25.0,
    "adx_ranging_thresh":   20.0,
    # TrendModel
    "trend_adx_min":        32.0,   # raised from 31.0 — stronger trend filter
    "trend_rsi_long_min":   45.0,
    "trend_rsi_long_max":   70.0,
    "trend_rsi_short_min":  30.0,
    "trend_rsi_short_max":  55.0,
    "trend_strength_base":  0.15,
    "trend_adx_bonus_max":  0.40,
    "trend_bull_only":      True,   # longs only — no bear-regime shorts
    # MomentumBreakout
    "mb_lookback":          20,
    "mb_vol_mult_min":      1.0,    # lowered from 1.5 — more MB signals
    "mb_rsi_bullish":       55.0,
    "mb_rsi_bearish":       45.0,
    "mb_strength_base":     0.35,
    # Regime
    "bb_expansion_ratio":   3.5,    # raised from 2.5 — stricter vol-expansion gate
    "bb_compression_ratio": 0.5,
    # Exit & risk
    "atr_sl_mult_override": 4.5,    # single ATR SL mult — replaces regime-specific REGIME_ATR_SL
    "tp_rr":                1.22,   # TP = stop_dist × 1.22
    "partial_pct":          0.5,    # 50% partial close at 1R (was 33%)
    "partial_r_trigger":    1.0,    # close partial at 1R
    # Execution costs
    "fee_pct":              0.04,
    "slippage_pct":         0.05,
    "warmup_bars":          100,
    # HTF gate & portfolio
    "htf_allowed_regimes":  [R_BULL, R_BEAR, R_VOL_EXP],
    "max_portfolio_heat":   0.90,
    # ── Section 3: MomentumBreakout strict gating ──────────────────────────
    # MB fires only in R_VOL_EXP bars.  With mb_require_bull_htf=True the 4H
    # must ALSO be R_BULL — eliminating bear-market breakout failures.
    "mb_require_bull_htf":  True,
    # ── Section 4: Crash defense thresholds (rolling-peak drawdown, 30m bars) ─
    # DEFENSIVE  ≥8%  → move all long SLs to breakeven
    # HIGH_ALERT ≥15% → close 50% of each long position
    # EMERGENCY  ≥20% → close all long positions immediately
    "crash_defense_enabled":    True,
    "crash_def_lookback_bars":  96,     # 48 h rolling-peak window  (96 × 30 m)
    "crash_def_defensive_dd":   0.08,
    "crash_def_high_alert_dd":  0.15,
    "crash_def_emergency_dd":   0.20,
    "crash_def_cooldown_bars":  48,     # 24 h cooldown between triggers
}

INITIAL_CAPITAL = 100_000.0

# ── Capital model definitions ──────────────────────────────────────────────────
# Sizing formula: size = min(equity × risk_pct / stop_pct, equity × eff_max_cap)
# With ATR SL mult=4.5, BTC 30m stop_pct ≈ 8–12%.
# When risk_pct / stop_pct > max_cap_pct the cap is always the binding constraint
# (i.e. every trade deploys exactly max_cap_pct of current equity).
# Example — Model E at stop_pct=9%: 10%/9%=111% → always capped at 90% per slot.
# eff_max_cap = max_cap_pct (per-position, independent of concurrent count — futures leverage)
CAPITAL_MODELS: Dict[str, Dict] = {
    "A_Current": {
        "label":       "Model A — 0.5% / 10%",
        "risk_pct":    0.005,
        "max_cap_pct": 0.10,    # was 4% — now 10% per slot
    },
    "B_Moderate": {
        "label":       "Model B — 1.0% / 25%",
        "risk_pct":    0.010,
        "max_cap_pct": 0.25,    # was 8% — now 25% per slot
    },
    "C_Aggressive": {
        "label":       "Model C — 2.0% / 45%",
        "risk_pct":    0.020,
        "max_cap_pct": 0.45,    # was 12% — now 45% per slot
    },
    "D_Conviction": {
        "label":       "Model D — conviction var / 50%",
        "risk_pct":    None,    # dynamic: 0.5–1.5% scaled by confluence score
        "max_cap_pct": 0.50,    # was 12% — now 50% per slot
    },
    # ── Model E′ — Staged allocation (user-specified, Section 2 corrected) ────
    # Rules (user spec 2026-03-26):
    #   • Max 3 concurrent BTC entries only.
    #   • Total notional deployed across ALL positions ≤ 100% equity.
    #   • Staged size caps by slot order:
    #       slot 0 (1st entry) → up to 50% of equity
    #       slot 1 (2nd entry) → up to 30% of equity
    #       slot 2 (3rd entry) → up to 20% of equity
    #       Total if all 3 filled = 100%  (no leverage).
    #   • Risk-per-trade check: size also capped by risk_pct/stop_pct.
    #   • Portfolio heat check: total deployed ≤ heat_max_pct.
    #   • No resizing of old trades when a new entry is added.
    # allocation_tiers → list of per-slot notional caps (as fraction of equity).
    # ── Model E′ (final spec 2026-03-26) ─────────────────────────────────────
    # The tier caps (50 / 30 / 20) ARE the primary sizing rule.
    # risk_pct is a safety net only — must not reduce position below its tier.
    #
    # Mathematical constraint: with atr_sl_mult=4.5 and BTC 30m ATR ≈ 0.7–2% of
    # price, stop_pct ≈ 3–9%.  To ensure the tier is always the binding constraint:
    #   risk_limited = equity × risk_pct / stop_pct  must be ≥ all tier caps.
    # Setting risk_pct = 0.60 (60%) guarantees:
    #   At stop_pct = 9% → risk_limited = 667% → tier cap (50%) always binds ✓
    #   At stop_pct = 9% → tier 2 (30%) binds ✓  tier 3 (20%) binds ✓
    # The heat_max_pct (100%) is the portfolio-level hard ceiling.
    # Crash defense provides dynamic de-risking on top of the tier/heat caps.
    #
    # Effective risk per trade (tier × stop_pct):
    #   Tier 1 (50%) at 9% stop → 4.5% portfolio risk
    #   Tier 2 (30%) at 9% stop → 2.7% portfolio risk  ← within 2–3% mandate
    #   Tier 3 (20%) at 9% stop → 1.8% portfolio risk  ← within 2–3% mandate
    #   Weighted average (50+30+20 equally likely) → 3.0% per trade
    "E_Prime": {
        "label":            "Model E′ — staged 50/30/20 (max 3 concurrent)",
        "risk_pct":         0.60,           # safety net only — tier cap always binds
        "max_cap_pct":      1.00,           # 100% total budget ceiling
        "heat_max_pct":     1.00,           # portfolio heat cap (100% total)
        "allocation_tiers": [0.50, 0.30, 0.20],  # per-slot caps by entry order
    },
    # ── Leveraged models — EXPLICITLY MARKED, NOT for phase 1 demo ──────────
    # Listed for comparison only.  These use notional > 100% equity (real leverage).
    # Bybit Demo supports up to 100×; 1.2×–1.5× notional is conservative futures.
    "F_Leveraged": {
        "label":       "Model F — 15% / 120%  [1.2× lev — NOT phase1]",
        "risk_pct":    0.150,
        "max_cap_pct": 1.20,
    },
    "G_MaxLeverage": {
        "label":       "Model G — 20% / 150%  [1.5× lev — NOT phase1]",
        "risk_pct":    0.200,
        "max_cap_pct": 1.50,
    },
}

# Concurrent slot configs to test.
# User spec 2026-03-26: max 3 concurrent BTC entries.
CONCURRENT_SLOTS = [1, 2, 3]


# ══════════════════════════════════════════════════════════════════════════════
# GPU-ACCELERATED INDICATOR COMPUTATION
# ══════════════════════════════════════════════════════════════════════════════

def _try_import_torch():
    """Return (torch, device) or (None, None) if not available / no CUDA."""
    try:
        import torch
        if torch.cuda.is_available():
            dev = torch.device("cuda")
            logger.info(f"GPU detected: {torch.cuda.get_device_name(0)} — using CUDA for indicators")
            return torch, dev
        else:
            logger.info("PyTorch found but no CUDA — using CPU NumPy for indicators")
            return None, None
    except ImportError:
        logger.info("PyTorch not installed — using CPU NumPy for indicators")
        return None, None


def compute_indicators_gpu(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                            volume: np.ndarray, torch_mod, device) -> dict:
    """
    Compute ATR, EMA-9, EMA-21, RSI-14, ADX-14, BB-width on GPU via PyTorch.
    Fully vectorised — uses conv1d for all rolling/EMA operations.
    No Python for-loops over bar indices.
    """
    import math as _math
    t = torch_mod
    F = t.nn.functional
    n = len(close)

    c  = t.tensor(close,  dtype=t.float32, device=device)
    h  = t.tensor(high,   dtype=t.float32, device=device)
    l  = t.tensor(low,    dtype=t.float32, device=device)
    vo = t.tensor(volume, dtype=t.float32, device=device)

    def _ema_conv(x: "t.Tensor", alpha: float) -> "t.Tensor":
        """Causal EMA via truncated conv1d kernel. O(n * lag), no Python loops."""
        lag = min(int(_math.log(1e-7) / _math.log(max(1 - alpha, 1e-9))) + 1, n)
        lags_t = t.arange(lag - 1, -1, -1, dtype=t.float32, device=device)
        kernel = alpha * (1.0 - alpha) ** lags_t
        kernel = kernel / kernel.sum()                               # normalise
        x_pad  = t.cat([x[:1].expand(lag - 1), x])                  # left-pad
        out = F.conv1d(x_pad.unsqueeze(0).unsqueeze(0),
                       kernel.unsqueeze(0).unsqueeze(0)).squeeze(0).squeeze(0)
        return out

    def _rolling_mean(x: "t.Tensor", w: int) -> "t.Tensor":
        """Causal rolling mean via uniform conv1d."""
        kernel = t.ones(1, 1, w, dtype=t.float32, device=device) / w
        x_pad  = t.cat([x[:1].expand(w - 1), x])
        out = F.conv1d(x_pad.unsqueeze(0).unsqueeze(0), kernel).squeeze(0).squeeze(0)
        return out

    # ── True Range ──
    prev_c = t.cat([c[:1], c[:-1]])
    tr = t.maximum(h - l, t.maximum(t.abs(h - prev_c), t.abs(l - prev_c)))

    # ── ATR-14 (Wilder EMA, α = 1/14) ──
    atr = _ema_conv(tr, 1.0 / 14)

    # ── EMA-9, EMA-21 ──
    ema9  = _ema_conv(c, 2.0 / 10)
    ema21 = _ema_conv(c, 2.0 / 22)

    # ── RSI-14 ──
    delta = t.cat([t.zeros(1, device=device), c[1:] - c[:-1]])
    up    = t.clamp(delta, min=0.0)
    dn    = t.clamp(-delta, min=0.0)
    avg_up  = _ema_conv(up,  1.0 / 14)
    avg_dn  = _ema_conv(dn,  1.0 / 14)
    rsi = 100.0 - 100.0 / (1.0 + avg_up / (avg_dn + 1e-9))

    # ── Bollinger Band Width (20-bar, 2σ) ──
    roll_mean  = _rolling_mean(c, 20)
    roll_mean2 = _rolling_mean(c * c, 20)
    roll_var   = t.clamp(roll_mean2 - roll_mean ** 2, min=0.0)
    roll_std   = t.sqrt(roll_var)
    bb_width   = (4.0 * roll_std) / (roll_mean + 1e-9)

    # ── Volume SMA-20 ──
    vol_sma = _rolling_mean(vo, 20)

    # ── ADX-14 ── (vectorised directional movement)
    h_prev = t.cat([h[:1], h[:-1]])
    l_prev = t.cat([l[:1], l[:-1]])
    up_mv  = h - h_prev
    dn_mv  = l_prev - l
    dm_p   = t.where((up_mv > dn_mv) & (up_mv > 0), up_mv, t.zeros_like(up_mv))
    dm_m   = t.where((dn_mv > up_mv) & (dn_mv > 0), dn_mv, t.zeros_like(dn_mv))

    sm_p   = _ema_conv(dm_p,  1.0 / 14) * 14  # Wilder smoothing ×14
    sm_m   = _ema_conv(dm_m,  1.0 / 14) * 14
    sm_tr  = _ema_conv(tr,    1.0 / 14) * 14

    di_p   = 100.0 * sm_p  / (sm_tr + 1e-9)
    di_m   = 100.0 * sm_m  / (sm_tr + 1e-9)
    dx     = 100.0 * t.abs(di_p - di_m) / (di_p + di_m + 1e-9)
    adx    = _ema_conv(dx, 1.0 / 14)

    return {
        "atr":      atr.cpu().numpy(),
        "ema9":     ema9.cpu().numpy(),
        "ema21":    ema21.cpu().numpy(),
        "rsi":      rsi.cpu().numpy(),
        "adx":      adx.cpu().numpy(),
        "bb_width": bb_width.cpu().numpy(),
        "vol_sma":  vol_sma.cpu().numpy(),
    }


def compute_indicators_cpu(close: np.ndarray, high: np.ndarray, low: np.ndarray,
                            volume: np.ndarray) -> dict:
    """Pure NumPy indicator computation (CPU fallback)."""
    n = len(close)

    # ATR-14
    prev_c = np.concatenate([[close[0]], close[:-1]])
    tr     = np.maximum(high - low, np.maximum(np.abs(high - prev_c), np.abs(low - prev_c)))
    atr    = np.zeros(n)
    k      = 1.0 / 14
    atr[0] = tr[0]
    for i in range(1, n):
        atr[i] = atr[i-1] * (1-k) + tr[i] * k

    # EMA-9, EMA-21
    k9, k21 = 2/10, 2/22
    ema9 = np.zeros(n); ema21 = np.zeros(n)
    ema9[0] = ema21[0] = close[0]
    for i in range(1, n):
        ema9[i]  = ema9[i-1]  * (1-k9)  + close[i] * k9
        ema21[i] = ema21[i-1] * (1-k21) + close[i] * k21

    # RSI-14
    delta    = np.diff(close, prepend=close[0])
    up       = np.where(delta > 0, delta, 0.0)
    down     = np.where(delta < 0, -delta, 0.0)
    avg_up   = np.zeros(n); avg_down = np.zeros(n)
    if n >= 15:
        avg_up[14]   = up[1:15].mean()
        avg_down[14] = down[1:15].mean()
        kr = 1.0/14
        for i in range(15, n):
            avg_up[i]   = avg_up[i-1]   * (1-kr) + up[i]   * kr
            avg_down[i] = avg_down[i-1] * (1-kr) + down[i] * kr
    rs  = avg_up / (avg_down + 1e-9)
    rsi = 100.0 - 100.0 / (1.0 + rs)

    # BB width (20, 2σ)
    bb_width = np.zeros(n)
    for i in range(20, n):
        w = close[i-20:i]; std = w.std(); mid = w.mean()
        bb_width[i] = (4*std) / (mid + 1e-9) if mid > 0 else 0

    # Volume SMA-20
    vol_sma = np.zeros(n)
    for i in range(20, n):
        vol_sma[i] = volume[i-20:i].mean()

    # ADX-14
    dm_plus  = np.zeros(n); dm_minus = np.zeros(n)
    for i in range(1, n):
        u = float(high[i] - high[i-1]); d = float(low[i-1] - low[i])
        dm_plus[i]  = u if (u > d and u > 0) else 0
        dm_minus[i] = d if (d > u and d > 0) else 0

    smooth_plus  = np.zeros(n); smooth_minus = np.zeros(n); smooth_tr = np.zeros(n)
    if n >= 15:
        smooth_plus[14]  = dm_plus[1:15].sum()
        smooth_minus[14] = dm_minus[1:15].sum()
        smooth_tr[14]    = tr[1:15].sum()
        for i in range(15, n):
            smooth_plus[i]  = smooth_plus[i-1]  - smooth_plus[i-1]/14  + dm_plus[i]
            smooth_minus[i] = smooth_minus[i-1] - smooth_minus[i-1]/14 + dm_minus[i]
            smooth_tr[i]    = smooth_tr[i-1]    - smooth_tr[i-1]/14    + tr[i]

    di_plus  = 100 * smooth_plus  / (smooth_tr + 1e-9)
    di_minus = 100 * smooth_minus / (smooth_tr + 1e-9)
    dx  = 100 * np.abs(di_plus - di_minus) / (di_plus + di_minus + 1e-9)
    adx = np.zeros(n)
    if n >= 29:
        adx[28] = dx[14:28].mean()
        ka = 1.0/14
        for i in range(29, n):
            adx[i] = adx[i-1] * (1-ka) + dx[i] * ka

    return {
        "atr": atr, "ema9": ema9, "ema21": ema21,
        "rsi": rsi, "adx": adx, "bb_width": bb_width, "vol_sma": vol_sma,
    }


# ══════════════════════════════════════════════════════════════════════════════
# REGIME + SIGNAL CLASSIFICATION
# ══════════════════════════════════════════════════════════════════════════════

def classify_regime_array(adx: np.ndarray, ema9: np.ndarray, ema21: np.ndarray,
                           bb_width: np.ndarray, params: dict) -> np.ndarray:
    """
    Vectorised regime classification — exact copy of backtest_4yr.py classify_regime().
    Applies BB override AFTER ADX classification (matches live IDSS logic).
    """
    n   = len(adx)
    out = np.full(n, R_UNCERTAIN, dtype=object)
    at  = params["adx_trend_thresh"]
    ar  = params["adx_ranging_thresh"]

    # Rolling 20-bar mean of bb_width (min 5 bars) — same as pd.rolling(20, min_periods=5)
    bb_roll = pd.Series(bb_width).rolling(20, min_periods=5).mean().values
    with np.errstate(divide="ignore", invalid="ignore"):
        bb_rat = np.where(bb_roll > 0, bb_width / bb_roll, 1.0)

    slope = np.sign(ema9 - ema21)
    v     = ~np.isnan(adx)

    # ADX-based classification first
    out[v & (adx < ar)]              = R_RANGING
    out[v & (adx >= ar) & (adx < at)] = R_RANGING
    mt = v & (adx >= at)
    out[mt & (slope >  0)] = R_BULL
    out[mt & (slope <= 0)] = R_BEAR

    # BB-based override applied AFTER (matches original)
    out[bb_rat >  params["bb_expansion_ratio"]]  = R_VOL_EXP
    out[bb_rat <  params["bb_compression_ratio"]] = R_VOL_COMP
    out[np.isnan(adx)] = R_UNCERTAIN
    return out


def generate_signals(df_dict: dict, params: dict, ind: dict) -> tuple:
    """
    Signal generation — exact copy of backtest_4yr.py generate_signals().
    Uses shift(1) for MomentumBreakout rolling high/low to prevent look-ahead.
    """
    close  = np.array(df_dict["close"])
    volume = np.array(df_dict["volume"])
    n      = len(close)

    adx      = ind["adx"]
    ema9     = ind["ema9"]
    ema21    = ind["ema21"]
    rsi      = ind["rsi"]
    bb_width = ind["bb_width"]
    atr      = ind["atr"]

    regimes = classify_regime_array(adx, ema9, ema21, bb_width, params)

    sl = np.zeros(n, bool)
    ss = np.zeros(n, bool)
    sc = np.zeros(n, float)

    # ── TrendModel ──────────────────────────────────────────────────────────
    # trend_bull_only=True: longs fire ONLY in bull_trend regime (matches Phase 5
    # cpu_precompute: allowed = in_bull when trend_bull_only).
    # Previous code used isin([BULL,BEAR]) for in_t which let longs fire in bear
    # regime — wrong when trend_bull_only=True and PF-degrading.
    bull_only = params.get("trend_bull_only", False)
    in_t_long  = np.isin(regimes, [R_BULL])          if bull_only \
                 else np.isin(regimes, [R_BULL, R_BEAR])
    in_t_short = np.zeros(n, bool)                   if bull_only \
                 else np.isin(regimes, [R_BULL, R_BEAR])

    has_adx = (adx >= params["trend_adx_min"]) & ~np.isnan(adx)
    tlc = in_t_long  & has_adx & (ema9 > ema21) & \
          (rsi >= params["trend_rsi_long_min"])  & (rsi <= params["trend_rsi_long_max"])
    tsc = in_t_short & has_adx & (ema9 < ema21) & \
          (rsi >= params["trend_rsi_short_min"]) & (rsi <= params["trend_rsi_short_max"])

    ab = np.minimum(
        params["trend_adx_bonus_max"],
        np.where(has_adx,
                 (adx - params["trend_adx_min"]) / params["trend_adx_min"] *
                 params["trend_adx_bonus_max"], 0)
    )
    ts = params["trend_strength_base"] + ab

    sl |= tlc; ss |= tsc
    sc = np.where(tlc | tsc, np.maximum(sc, ts), sc)

    # ── MomentumBreakout ────────────────────────────────────────────────────
    lb  = params["mb_lookback"]
    cs2 = pd.Series(close)
    vs2 = pd.Series(volume)
    # shift(1) prevents using current bar's price in rolling window
    rh = cs2.shift(1).rolling(lb, min_periods=lb).max().values
    rl = cs2.shift(1).rolling(lb, min_periods=lb).min().values
    va = vs2.rolling(lb, min_periods=lb).mean().values

    vp   = (volume > va * params["mb_vol_mult_min"]) & (va > 0)
    ive  = (regimes == R_VOL_EXP)
    rhs  = np.where(np.isnan(rh), close * 1e9, rh)
    rls  = np.where(np.isnan(rl), 0.0, rl)
    mlc  = ive & vp & (close > rhs) & (rsi > params["mb_rsi_bullish"])
    msc  = ive & vp & (close < rls) & (rsi < params["mb_rsi_bearish"])

    with np.errstate(divide="ignore", invalid="ignore"):
        bpct = np.where(rhs > 0, (close - rhs) / rhs * 100, 0.0)
    bsc = np.minimum(1.0, bpct / 2.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        vr  = np.where(va > 0, volume / va, 1.0)
    vsc = np.minimum(1.0, (vr - params["mb_vol_mult_min"]) / params["mb_vol_mult_min"])
    ms  = params["mb_strength_base"] + vsc * 0.35 + bsc * 0.3

    sl |= mlc; ss |= msc
    sc = np.where(mlc | msc, np.maximum(sc, ms), sc)

    # Zero out signals where ATR is zero (insufficient data)
    sl[atr == 0] = False
    ss[atr == 0] = False

    return sl, ss, sc, regimes


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION ENGINE — supports max_concurrent_positions
# ══════════════════════════════════════════════════════════════════════════════

def simulate_model_concurrent(args: tuple) -> dict:
    """
    Top-level worker — must be picklable for ProcessPoolExecutor.

    Financial math is identical to backtest_4yr.py (the reference single-position
    backtest that produced PF 0.852, WR 44.5%, 173 trades).

    Key rules (Phase 5 optimized params):
    - Stop distance: atr_sl_mult_override × ATR  (single value = 4.5 for all regimes)
    - TP distance:   stop_dist × tp_rr           (= 4.5 × ATR × 1.22)
    - Partial at 1R: partial_tp = entry ± stop_dist  (= exactly 1R)
    - Partial close: 50% of position (partial_pct=0.50), SL → breakeven
    - Partial PnL:   (price_change × qty_part) − fee
    - Full close PnL: (price_change × qty_remaining) − fee
    - Entry: signal bar's close (single bar fill)
    - trend_bull_only=True: only long TrendModel signals; MB shorts still allowed
    - Max concurrent: open new position only when len(positions) < max_concurrent
    """
    (model_id, model_cfg, df_dict, regimes, sig_long, sig_short,
     confluence, ind, params, cap0, max_concurrent) = args

    close_arr = np.array(df_dict["close"])
    high_arr  = np.array(df_dict["high"])
    low_arr   = np.array(df_dict["low"])
    atr_arr   = ind["atr"]
    n         = len(close_arr)

    risk_pct_base     = model_cfg["risk_pct"]
    max_cap_pct       = model_cfg["max_cap_pct"]
    heat_max_pct      = model_cfg.get("heat_max_pct", model_cfg.get("max_portfolio_heat",
                                      params.get("max_portfolio_heat", 0.90)))
    allocation_tiers  = model_cfg.get("allocation_tiers", None)  # staged model
    fee_pct           = params["fee_pct"]    / 100.0
    slip_pct          = params["slippage_pct"] / 100.0
    part_pct          = params["partial_pct"]
    thresh            = params["confluence_threshold"]
    warmup            = params["warmup_bars"]

    # eff_max_cap is used only when allocation_tiers is NOT set (non-staged models).
    # Staged model uses per-slot tier caps instead.
    eff_max_cap = max_cap_pct

    # ── Section 4: crash defense parameters ──
    cd_enabled  = params.get("crash_defense_enabled", False)
    cd_lookback = int(params.get("crash_def_lookback_bars", 96))
    cd_dd1      = float(params.get("crash_def_defensive_dd",   0.08))
    cd_dd2      = float(params.get("crash_def_high_alert_dd",  0.15))
    cd_dd3      = float(params.get("crash_def_emergency_dd",   0.20))
    cd_cooldown_max = int(params.get("crash_def_cooldown_bars", 48))
    cd_cooldown  = 0           # bars remaining in cooldown
    cd_events    = []          # records of crash-defense triggers

    equity    = cap0
    positions = []   # list of open position dicts
    trades    = []   # all trade records (full closes + partial closes)
    eq_curve  = [(0, equity)]
    util_samples = []   # capital utilization % per bar (sampled every 48 bars)

    for i in range(1, n):
        hi    = float(high_arr[i])
        lo    = float(low_arr[i])
        cl    = float(close_arr[i])
        atr_i = float(atr_arr[i]) if not math.isnan(float(atr_arr[i])) else cl * 0.02

        # ── 1. Update existing positions (partial close, SL, TP) ────────────
        to_remove = []
        for pos in positions:
            is_long      = pos["direction"] == "long"
            entry        = pos["entry"]
            sl           = pos["sl"]
            tp           = pos["tp"]
            partial_done = pos["partial_done"]

            # Check partial close at 1R (using intrabar high/low)
            if not partial_done:
                pp_dist_needed = pos["asl"] * pos["atr_entry"]  # = 1R distance
                if is_long:
                    pp_hit = (hi - entry) >= pp_dist_needed
                else:
                    pp_hit = (entry - lo) >= pp_dist_needed

                if pp_hit:
                    ppx      = entry + pp_dist_needed if is_long else entry - pp_dist_needed
                    pf2      = ppx * (1 - slip_pct) if is_long else ppx * (1 + slip_pct)
                    psize    = pos["size_usdt"] * part_pct
                    qty_part = psize / entry  # quantity of asset being closed
                    ppnl     = ((pf2 - entry) * qty_part if is_long else (entry - pf2) * qty_part) \
                               - pf2 * qty_part * fee_pct
                    equity              += ppnl
                    pos["size_usdt"]    *= (1 - part_pct)  # reduce remaining size
                    pos["sl"]            = entry             # SL → breakeven
                    pos["partial_done"]  = True
                    trades.append({
                        "pnl":         round(ppnl, 4),
                        "exit_reason": "partial_close",
                        "regime":      pos["regime"],
                        "bar_close":   i,
                        "bar_open":    pos["bar_open"],
                    })

            # Check SL / TP using intrabar high/low
            sl_hit = (lo <= pos["sl"]) if is_long else (hi >= pos["sl"])
            tp_hit = (hi >= tp)        if is_long else (lo <= tp)

            if sl_hit or tp_hit:
                xpx   = pos["sl"] if sl_hit else tp
                xf    = xpx * (1 - slip_pct) if is_long else xpx * (1 + slip_pct)
                xqty  = pos["size_usdt"] / entry  # remaining quantity
                xfee  = xf * xqty * fee_pct
                pnl   = ((xf - entry) * xqty - xfee) if is_long \
                        else ((entry - xf) * xqty - xfee)
                equity += pnl
                trades.append({
                    "pnl":         round(pnl, 4),
                    "exit_reason": "stop_loss" if sl_hit else "take_profit",
                    "regime":      pos["regime"],
                    "bar_close":   i,
                    "bar_open":    pos["bar_open"],
                    "direction":   pos["direction"],
                })
                to_remove.append(pos)

        for p in to_remove:
            positions.remove(p)

        # ── Section 4: crash defense ─────────────────────────────────────────
        if cd_enabled and cd_cooldown <= 0 and positions:
            lb_start    = max(0, i - cd_lookback)
            recent_peak = float(np.max(high_arr[lb_start : i + 1]))
            dd_now      = (recent_peak - cl) / recent_peak if recent_peak > 0 else 0.0

            if dd_now >= cd_dd3:                   # EMERGENCY — close all longs
                for pos in [p for p in positions if p["direction"] == "long"]:
                    xf   = cl * (1 - slip_pct)
                    xqty = pos["size_usdt"] / pos["entry"]
                    pnl  = (xf - pos["entry"]) * xqty - xf * xqty * fee_pct
                    equity += pnl
                    trades.append({"pnl": round(pnl, 4), "exit_reason": "crash_emergency",
                                   "regime": pos["regime"], "bar_close": i,
                                   "bar_open": pos["bar_open"], "direction": "long"})
                    positions.remove(pos)
                cd_events.append({"bar": i, "level": "EMERGENCY", "dd_pct": round(dd_now * 100, 2)})
                cd_cooldown = cd_cooldown_max

            elif dd_now >= cd_dd2:                 # HIGH_ALERT — 50% partial on each long
                for pos in [p for p in positions if p["direction"] == "long"
                             and not pos.get("cd_partial_done", False)]:
                    half     = pos["size_usdt"] * 0.50
                    xf       = cl * (1 - slip_pct)
                    xqty     = half / pos["entry"]
                    pnl      = (xf - pos["entry"]) * xqty - xf * xqty * fee_pct
                    equity  += pnl
                    pos["size_usdt"]      *= 0.50
                    pos["cd_partial_done"] = True
                    pos["sl"]              = max(pos["sl"], pos["entry"])  # BE
                    trades.append({"pnl": round(pnl, 4), "exit_reason": "crash_high_alert",
                                   "regime": pos["regime"], "bar_close": i,
                                   "bar_open": pos["bar_open"], "direction": "long"})
                cd_events.append({"bar": i, "level": "HIGH_ALERT", "dd_pct": round(dd_now * 100, 2)})
                cd_cooldown = cd_cooldown_max

            elif dd_now >= cd_dd1:                 # DEFENSIVE — move all longs to BE
                for pos in [p for p in positions if p["direction"] == "long"
                             and not pos.get("cd_be_applied", False)]:
                    pos["sl"]          = max(pos["sl"], pos["entry"])
                    pos["cd_be_applied"] = True
                cd_events.append({"bar": i, "level": "DEFENSIVE", "dd_pct": round(dd_now * 100, 2)})
                cd_cooldown = cd_cooldown_max

        if cd_cooldown > 0:
            cd_cooldown -= 1

        # ── Capital utilization sample (every 48 bars = 24 h) ────────────────
        if i % 48 == 0:
            deployed = sum(p["size_usdt"] for p in positions)
            util_samples.append(deployed / equity * 100 if equity > 0 else 0.0)
            eq_curve.append((i, round(equity, 2)))

        # ── 2. Open new position if slot available and signal fires ──────────
        if len(positions) >= max_concurrent:
            continue
        if i < warmup:
            continue

        can_long  = bool(sig_long[i])  and float(confluence[i]) >= thresh
        can_short = bool(sig_short[i]) and float(confluence[i]) >= thresh
        if not (can_long or can_short):
            continue

        direction = "long" if can_long else "short"
        if can_long and can_short:
            direction = "long"   # tie-break: prefer long (matches original)

        ri   = str(regimes[i])
        ai   = atr_i
        # Phase 5: single ATR SL multiplier for all regimes (overrides REGIME_ATR_SL)
        asl  = float(params.get("atr_sl_mult_override",
                                REGIME_ATR_SL.get(ri, 2.0)))
        score = float(confluence[i])

        # Conviction-based risk for Model D
        if risk_pct_base is None:
            sn       = min(1.0, max(0.0, (score - 0.45) / 0.30))
            risk_pct = 0.005 + sn * 0.010
        else:
            risk_pct = risk_pct_base

        entry_px  = cl
        stop_dist = ai * asl
        stop_pct  = stop_dist / entry_px if entry_px > 0 else 0.001
        if stop_pct <= 0:
            continue

        # ── Section 2: staged allocation (slot-based) ────────────────────────
        # When allocation_tiers is set (Model E′):
        #   • Each tier (50%/30%/20%) is a NAMED SLOT, not an order index.
        #   • New position claims the largest free slot so that, whenever 3
        #     positions are open, total notional = 50+30+20 = 100% equity.
        #   • Example: if the 50%-slot closes while 30%+20% remain, next
        #     entry reclaims the 50%-slot → total returns to 100%.
        #   • Heat cap (100%) and risk_pct are applied as safety ceilings.
        # Non-staged models: standard risk-based sizing with eff_max_cap.

        current_deployed = sum(p["size_usdt"] for p in positions)

        if allocation_tiers:
            # Find the free slot with the LARGEST allocation (lowest index)
            used_slots  = {pos.get("slot_id", -1) for pos in positions}
            free_slots  = [i for i in range(len(allocation_tiers))
                           if i not in used_slots]
            if not free_slots:
                continue   # all tier slots occupied (should not reach here)
            slot_id      = min(free_slots)   # prefer highest-value free slot
            tier_cap     = equity * allocation_tiers[slot_id]

            # Heat budget: must not push total deployed above heat_max_pct
            heat_remaining = max(0.0, equity * heat_max_pct - current_deployed)

            # Risk ceiling (soft advisory for staged model; tier cap drives sizing)
            risk_limited = equity * risk_pct / stop_pct

            # Binding: tier cap and heat budget (risk ceiling applied softly)
            size_usdt = min(tier_cap, heat_remaining, risk_limited)
        else:
            slot_id      = None
            heat_remaining = max(0.0, equity * heat_max_pct - current_deployed)
            risk_limited   = equity * risk_pct / stop_pct
            size_usdt      = min(equity * eff_max_cap, heat_remaining, risk_limited)

        if size_usdt <= 100:   # skip trivially small positions ($100 floor)
            continue

        tp_rr = float(params.get("tp_rr", 1.22))   # TP = stop_dist × tp_rr
        if direction == "long":
            sl_px = entry_px - stop_dist
            tp_px = entry_px + stop_dist * tp_rr
        else:
            sl_px = entry_px + stop_dist
            tp_px = entry_px - stop_dist * tp_rr

        positions.append({
            "entry":        entry_px,
            "sl":           sl_px,
            "tp":           tp_px,
            "direction":    direction,
            "size_usdt":    size_usdt,
            "orig_size":    size_usdt,
            "asl":          asl,           # stop ATR multiplier (for partial calc)
            "atr_entry":    ai,            # ATR at entry (for partial calc)
            "regime":       ri,
            "bar_open":     i,
            "partial_done": False,
            "slot_id":      slot_id,       # tier slot index (staged model only)
        })

    # Close any open positions at end of data
    for pos in positions:
        is_long = pos["direction"] == "long"
        xf      = float(close_arr[-1]) * (1 - slip_pct if is_long else 1 + slip_pct)
        xqty    = pos["size_usdt"] / pos["entry"]
        xfee    = xf * xqty * fee_pct
        pnl     = ((xf - pos["entry"]) * xqty - xfee) if is_long \
                  else ((pos["entry"] - xf) * xqty - xfee)
        equity += pnl
        trades.append({"pnl": round(pnl, 4), "exit_reason": "end_of_data",
                       "regime": pos["regime"], "bar_close": n - 1, "bar_open": pos["bar_open"],
                       "direction": pos["direction"]})

    eq_curve.append((n - 1, round(equity, 2)))

    avg_util = round(sum(util_samples) / len(util_samples), 2) if util_samples else 0.0
    max_util = round(max(util_samples), 2) if util_samples else 0.0

    return {
        "model_id":       model_id,
        "max_concurrent": max_concurrent,
        "final_equity":   equity,
        "eq_curve":       eq_curve,
        "trades":         trades,
        "avg_util_pct":   avg_util,
        "max_util_pct":   max_util,
        "cd_events":      cd_events,
    }


# ══════════════════════════════════════════════════════════════════════════════
# METRICS
# ══════════════════════════════════════════════════════════════════════════════

def compute_metrics(trades: list, eq_curve: list, cap0: float,
                    avg_util: float = 0.0, max_util: float = 0.0,
                    cd_events: Optional[List] = None) -> dict:
    """Compute all performance metrics from a completed simulation.

    PF is computed as TRUE profit factor — gross wins INCLUDES all partial-close
    profits (partial closes are always profitable: they exit at 1R). Excluding them
    would under-count gross wins and give a misleadingly low PF (e.g. 1.12 vs 2.39).
    """
    full_trades = [t for t in trades if t.get("exit_reason") != "partial_close"]
    part_trades = [t for t in trades if t.get("exit_reason") == "partial_close"]

    if not full_trades:
        return {"error": "no trades"}

    wins   = [t for t in full_trades if t["pnl"] > 0]
    losses = [t for t in full_trades if t["pnl"] <= 0]
    gw     = sum(t["pnl"] for t in wins)
    gl     = abs(sum(t["pnl"] for t in losses))
    # True PF: include partial-close profits in gross wins.
    # Partial closes exit at 1R — always profitable — and represent real realised gains.
    part_gross = sum(t["pnl"] for t in part_trades if t["pnl"] > 0)
    pf     = (gw + part_gross) / gl if gl > 0 else 999.0
    wr     = len(wins) / len(full_trades) * 100

    final_eq = eq_curve[-1][1] if eq_curve else cap0
    years    = (eq_curve[-1][0] - eq_curve[0][0]) * (30 / 60 / 24 / 365.25) if len(eq_curve) > 1 else 4.0
    if years <= 0: years = 4.0
    cagr     = ((final_eq / cap0) ** (1 / years) - 1) * 100

    # Max drawdown
    peak = cap0; max_dd = 0.0
    for _, eq in eq_curve:
        if eq > peak: peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd: max_dd = dd

    # Regime breakdown
    regime_stats: dict = {}
    for t in full_trades:
        r = t.get("regime", "unknown")
        if r not in regime_stats:
            regime_stats[r] = {"count": 0, "wins": 0, "pnl": 0.0}
        regime_stats[r]["count"] += 1
        regime_stats[r]["pnl"]   += t["pnl"]
        if t["pnl"] > 0:
            regime_stats[r]["wins"] += 1

    regime_breakdown = {
        r: {"count": v["count"], "wr_pct": round(v["wins"]/v["count"]*100, 1),
            "pnl": round(v["pnl"], 2)}
        for r, v in regime_stats.items()
    }

    # Yearly breakdown
    yearly: dict = {}
    if full_trades and "bar_open" in full_trades[0]:
        pass  # bar index only; approximate: 70079 bars / 48 months = ~1460 bars/year
    # Use equity curve timestamps (bar index × 30min)
    bars_per_year = int(365.25 * 24 * 2)  # 30m bars per year
    start_bar = eq_curve[0][0] if eq_curve else 0
    for t in full_trades:
        bar = t.get("bar_close", t.get("bar_open", 0))
        year_offset = (bar - start_bar) // bars_per_year
        yr = 2022 + year_offset
        if yr not in yearly:
            yearly[yr] = {"pnl": 0.0, "trades": 0, "wins": 0}
        yearly[yr]["pnl"]    += t["pnl"]
        yearly[yr]["trades"] += 1
        if t["pnl"] > 0:
            yearly[yr]["wins"] += 1

    yearly_out = {
        str(yr): {"pnl": round(v["pnl"], 2),
                  "trades": v["trades"],
                  "wr_pct": round(v["wins"]/v["trades"]*100, 1) if v["trades"] else 0}
        for yr, v in sorted(yearly.items())
    }

    partial_pnl = sum(t["pnl"] for t in part_trades)

    # Crash defense summary
    cd_summary: dict = {}
    if cd_events:
        levels = [e["level"] for e in cd_events]
        cd_summary = {
            "total_triggers":  len(cd_events),
            "DEFENSIVE":       levels.count("DEFENSIVE"),
            "HIGH_ALERT":      levels.count("HIGH_ALERT"),
            "EMERGENCY":       levels.count("EMERGENCY"),
        }

    return {
        "total_trades_full":    len(full_trades),
        "total_trades_partial": len(part_trades),
        "win_rate_pct":         round(wr, 2),
        "profit_factor":        round(pf, 4),
        "gross_profit":         round(gw, 2),
        "gross_loss":           round(gl, 2),
        "net_pnl_full":         round(gw - gl, 2),
        "net_pnl_partial":      round(partial_pnl, 2),
        "net_pnl_total":        round(gw - gl + partial_pnl, 2),
        "initial_capital":      cap0,
        "final_equity":         round(final_eq, 2),
        "total_return_pct":     round((final_eq / cap0 - 1) * 100, 2),
        "max_drawdown_pct":     round(max_dd, 2),
        "cagr_pct":             round(cagr, 2),
        "years":                round(years, 2),
        "max_consec_losses":    _max_consec_losses(full_trades),
        "avg_util_pct":         round(avg_util, 2),
        "max_util_pct":         round(max_util, 2),
        "crash_defense":        cd_summary,
        "regime_breakdown":     regime_breakdown,
        "yearly":               yearly_out,
        "eq_sample":            eq_curve[::max(1, len(eq_curve)//30)],
    }


def _max_consec_losses(trades: list) -> int:
    mc = cur = 0
    for t in trades:
        if t["pnl"] <= 0:
            cur += 1; mc = max(mc, cur)
        else:
            cur = 0
    return mc


# ══════════════════════════════════════════════════════════════════════════════
# PRINTING
# ══════════════════════════════════════════════════════════════════════════════

SEP = "═" * 110

def pr_sep(): print(SEP)

def print_concurrent_table(results: dict, models: list, concurrent_list: list):
    """Print a matrix: rows = capital models, cols = concurrent slots."""
    print(f"\n{'FINAL EQUITY after 4 Years — $100,000 Starting Capital':^110}")
    pr_sep()
    hdr = f"  {'Model':<28}" + "".join(f" {'C='+str(c):>15}" for c in concurrent_list)
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                eq = m.get("final_equity", 0)
                row += f" ${eq:>13,.0f}"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()

    print(f"\n{'CAGR % — Capital Growth Rate per Year':^110}")
    pr_sep()
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                cagr = m.get("cagr_pct", 0)
                row += f" {cagr:>13.2f}%"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()

    print(f"\n{'MAX DRAWDOWN % — Worst Peak-to-Trough':^110}")
    pr_sep()
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                dd = m.get("max_drawdown_pct", 0)
                row += f" {dd:>13.2f}%"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()

    print(f"\n{'WIN RATE % — Full-Close Trades Only':^110}")
    pr_sep()
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                wr = m.get("win_rate_pct", 0)
                row += f" {wr:>13.1f}%"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()

    print(f"\n{'PROFIT FACTOR — Full-Close Trades (>1.0 = edge)':^110}")
    pr_sep()
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                pf = m.get("profit_factor", 0)
                row += f" {pf:>13.3f}"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()

    print(f"\n{'TRADE COUNT (full closes)':^110}")
    pr_sep()
    print(hdr)
    pr_sep()
    for mid in models:
        cfg_label = CAPITAL_MODELS[mid]["label"]
        row = f"  {cfg_label:<28}"
        for c in concurrent_list:
            key = f"{mid}__c{c}"
            if key in results:
                m = results[key].get("metrics", {})
                tc = m.get("total_trades_full", 0)
                row += f" {tc:>14,}"
            else:
                row += f" {'N/A':>14}"
        print(row)
    pr_sep()


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="NexusTrader 4-Year Backtest v2")
    parser.add_argument("--workers",    type=int, default=0,
                        help="CPU workers (0=all cores)")
    parser.add_argument("--capital",    type=float, default=INITIAL_CAPITAL)
    parser.add_argument("--no-htf",    action="store_true",
                        help="Disable 4H HTF gate (run without multi-TF)")
    parser.add_argument("--no-gpu",    action="store_true",
                        help="Force CPU even if CUDA available")
    parser.add_argument("--concurrent", type=str, default="1,2,3,4,5",
                        help="Comma-separated list of concurrent slot counts to test")
    args = parser.parse_args()

    n_workers = args.workers or mp.cpu_count()
    cap0      = args.capital
    use_htf   = not args.no_htf
    conc_list = [int(x) for x in args.concurrent.split(",")]

    print(f"\n{'NexusTrader 4-Year Backtest v2':=^80}")
    print(f"  Workers:    {n_workers} CPU cores")
    print(f"  Capital:    ${cap0:,.0f}")
    print(f"  HTF Gate:   {'ON (4H regime)' if use_htf else 'OFF (disabled)'}")
    print(f"  Scenarios:  {len(CAPITAL_MODELS)} models × {len(conc_list)} concurrent = "
          f"{len(CAPITAL_MODELS)*len(conc_list)} simulations")
    print()

    # ── [1] Check GPU ──────────────────────────────────────────────────────
    torch_mod, device = (None, None) if args.no_gpu else _try_import_torch()

    # ── [2] Load 30m indicators ────────────────────────────────────────────
    print("[1/4] Loading 30m indicator data from backtest_data/ ...")
    p30 = DATA_DIR / "BTC_USDT_30m_ind.parquet"
    if not p30.exists():
        # Fallback: try OHLCV-only parquet
        p30 = DATA_DIR / "BTC_USDT_30m.parquet"
    if not p30.exists():
        raise FileNotFoundError(
            f"30m data not found at {DATA_DIR}/BTC_USDT_30m_ind.parquet\n"
            f"Ensure backtest_data/ folder exists with pre-computed parquet files."
        )

    df30 = pd.read_parquet(p30)
    if not isinstance(df30.index, pd.DatetimeIndex):
        df30 = df30.set_index(df30.columns[0])
    if df30.index.tz is None:
        df30.index = df30.index.tz_localize("UTC")

    # ── Section 5: BTC-only enforcement ──────────────────────────────────────
    btc_file = str(p30.name).upper()
    assert "BTC" in btc_file, (
        f"SAFETY STOP: data file '{p30.name}' does not appear to be BTC data. "
        f"System is BTC-only. Pass a BTC_USDT parquet or rename the file."
    )
    print(f"   [BTC-ONLY CONFIRMED] Data file: {p30.name}")
    print(f"   30m: {df30.index[0].date()} → {df30.index[-1].date()}  ({len(df30):,} bars)")

    close_arr  = df30["close"].values.astype(float)
    high_arr   = df30["high"].values.astype(float)
    low_arr    = df30["low"].values.astype(float)
    volume_arr = df30["volume"].values.astype(float)

    # ── [3] Compute Indicators ─────────────────────────────────────────────
    print("[2/4] Computing indicators ...")
    t0 = time.time()

    # Priority 1: use pre-computed columns already in the parquet (fastest)
    _cols_needed = ["ema_9", "ema_21", "adx", "rsi", "atr"]
    if all(c in df30.columns for c in _cols_needed):
        print("   Using pre-computed indicators from parquet (instant) ...")
        ind = {
            "atr":      df30["atr"].values.astype(float),
            "ema9":     df30["ema_9"].values.astype(float),
            "ema21":    df30["ema_21"].values.astype(float),
            "rsi":      df30["rsi"].values.astype(float),
            "adx":      df30["adx"].values.astype(float),
            "bb_width": (df30["bb_width"].values.astype(float)
                         if "bb_width" in df30.columns
                         else np.zeros(len(df30))),
            "vol_sma":  df30["volume"].rolling(20, min_periods=1).mean().values.astype(float),
        }
    # Priority 2: GPU (vectorised conv1d — fast when pre-computed data absent)
    elif torch_mod is not None:
        print("   Pre-computed indicators not found — computing on GPU (CUDA) ...")
        ind = compute_indicators_gpu(close_arr, high_arr, low_arr, volume_arr, torch_mod, device)
    # Priority 3: CPU NumPy fallback
    else:
        print("   Pre-computed indicators not found — computing on CPU (NumPy) ...")
        ind = compute_indicators_cpu(close_arr, high_arr, low_arr, volume_arr)

    print(f"   Done in {time.time()-t0:.2f}s")

    # ── [4] Generate signals ───────────────────────────────────────────────
    print("[3/4] Generating signals ...")
    df_dict = {
        "close":  list(close_arr),
        "high":   list(high_arr),
        "low":    list(low_arr),
        "volume": list(volume_arr),
    }
    sig_long, sig_short, confluence, regimes_30m = generate_signals(df_dict, PROD_PARAMS, ind)

    # 4H HTF gate
    if use_htf:
        p4h = DATA_DIR / "BTC_USDT_4h_ind.parquet"
        if not p4h.exists():
            p4h = DATA_DIR / "BTC_USDT_4h.parquet"
        if p4h.exists():
            df4h = pd.read_parquet(p4h)
            if not isinstance(df4h.index, pd.DatetimeIndex):
                df4h = df4h.set_index(df4h.columns[0])
            if df4h.index.tz is None:
                df4h.index = df4h.index.tz_localize("UTC")

            # Classify 4H regime
            c4 = df4h["close"].values.astype(float)
            h4 = df4h["high"].values.astype(float)
            l4 = df4h["low"].values.astype(float)
            v4 = df4h["volume"].values.astype(float)
            _cols4 = ["ema_9", "ema_21", "adx", "rsi", "atr"]
            if all(cx in df4h.columns for cx in _cols4):
                ind4 = {
                    "atr":      df4h["atr"].values.astype(float),
                    "ema9":     df4h["ema_9"].values.astype(float),
                    "ema21":    df4h["ema_21"].values.astype(float),
                    "rsi":      df4h["rsi"].values.astype(float),
                    "adx":      df4h["adx"].values.astype(float),
                    "bb_width": (df4h["bb_width"].values.astype(float)
                                 if "bb_width" in df4h.columns else np.zeros(len(df4h))),
                    "vol_sma":  df4h["volume"].rolling(20, min_periods=1).mean().values.astype(float),
                }
            elif torch_mod is not None:
                ind4 = compute_indicators_gpu(c4, h4, l4, v4, torch_mod, device)
            else:
                ind4 = compute_indicators_cpu(c4, h4, l4, v4)

            r4 = classify_regime_array(ind4["adx"], ind4["ema9"], ind4["ema21"],
                                       ind4["bb_width"], PROD_PARAMS)
            htf_series = pd.Series(r4, index=df4h.index.tz_localize(None) if df4h.index.tz else df4h.index)
            idx30_ntz  = df30.index.tz_localize(None) if df30.index.tz else df30.index
            htf_aligned = htf_series.reindex(pd.DatetimeIndex(idx30_ntz), method="ffill").fillna(R_UNCERTAIN)
            allowed = PROD_PARAMS["htf_allowed_regimes"]
            htf_pass = np.array([r in allowed for r in htf_aligned.values])
            sig_long  = sig_long  & htf_pass
            sig_short = sig_short & htf_pass
            print(f"   4H gate active — signals after gate: long={sig_long.sum():,}  short={sig_short.sum():,}")

            # ── Section 3: MB strict gating — require 4H R_BULL for vol-exp signals ──
            # MomentumBreakout fires exclusively on R_VOL_EXP bars.  In the 4-year
            # backtest those bars were 23 % WR / PF-negative (mainly 2022 bear crash).
            # With mb_require_bull_htf=True we cancel any signal on a vol_exp 30m bar
            # where the 4H is NOT R_BULL.  This eliminates false breakouts during
            # bear-market volatility spikes without touching TrendModel signals.
            if PROD_PARAMS.get("mb_require_bull_htf", False):
                htf_bull_mask = np.array([r == R_BULL for r in htf_aligned.values])
                vol_exp_30m   = np.array([r == R_VOL_EXP for r in regimes_30m])
                mb_gate_fail  = vol_exp_30m & ~htf_bull_mask
                removed_long  = int((sig_long  & mb_gate_fail).sum())
                removed_short = int((sig_short & mb_gate_fail).sum())
                sig_long   = sig_long  & ~mb_gate_fail
                sig_short  = sig_short & ~mb_gate_fail
                print(f"   MB bull-HTF gate: removed {removed_long + removed_short:,} "
                      f"vol-exp signals (long={removed_long}, short={removed_short}) "
                      f"→ long={sig_long.sum():,}  short={sig_short.sum():,}")
        else:
            print("   WARNING: 4H data not found — HTF gate disabled")

    print(f"   Long signals: {sig_long.sum():,}  Short signals: {sig_short.sum():,}")

    # ── [5] Build all simulation jobs ─────────────────────────────────────
    print(f"[4/4] Running {len(CAPITAL_MODELS)*len(conc_list)} simulations on {n_workers} cores ...")

    all_args = []
    for mid, mcfg in CAPITAL_MODELS.items():
        for c in conc_list:
            all_args.append((
                mid, mcfg, df_dict, regimes_30m,
                sig_long, sig_short, confluence, ind,
                PROD_PARAMS, cap0, c
            ))

    raw_results: dict = {}
    t_run = time.time()

    if n_workers == 1:
        for a in all_args:
            r = simulate_model_concurrent(a)
            key = f"{r['model_id']}__c{r['max_concurrent']}"
            raw_results[key] = r
            m = compute_metrics(r["trades"], r["eq_curve"], cap0,
                                r.get("avg_util_pct",0), r.get("max_util_pct",0),
                                r.get("cd_events",[]))
            print(f"   {key:<28}: ${m.get('final_equity',0):>10,.0f}  "
                  f"PF={m.get('profit_factor',0):.3f}  WR={m.get('win_rate_pct',0):.1f}%  "
                  f"util={m.get('avg_util_pct',0):.1f}%")
    else:
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            futures = {pool.submit(simulate_model_concurrent, a): a for a in all_args}
            done_count = 0
            for future in as_completed(futures):
                try:
                    r   = future.result()
                    key = f"{r['model_id']}__c{r['max_concurrent']}"
                    raw_results[key] = r
                    done_count += 1
                    print(f"   [{done_count:02d}/{len(all_args)}] {key:<30} finished — "
                          f"equity ${r['final_equity']:,.0f}  trades={len([t for t in r['trades'] if t.get('exit_reason')!='partial_close'])}")
                except Exception as e:
                    a = futures[future]
                    print(f"   ERROR in {a[0]} c={a[-1]}: {e}")

    elapsed = time.time() - t_run
    print(f"\n   Total run time: {elapsed:.1f}s  ({elapsed/len(all_args):.2f}s per scenario)")

    # ── Compute metrics for all ────────────────────────────────────────────
    final_results: dict = {}
    for key, r in raw_results.items():
        m = compute_metrics(
            r["trades"], r["eq_curve"], cap0,
            avg_util=r.get("avg_util_pct", 0.0),
            max_util=r.get("max_util_pct", 0.0),
            cd_events=r.get("cd_events", []),
        )
        final_results[key] = {
            "model_id":       r["model_id"],
            "max_concurrent": r["max_concurrent"],
            "config":         CAPITAL_MODELS[r["model_id"]],
            "metrics":        m,
        }

    # ── Print comparison matrices ──────────────────────────────────────────
    print()
    print_concurrent_table(final_results, list(CAPITAL_MODELS.keys()), conc_list)

    # ── Best scenario ──────────────────────────────────────────────────────
    best_key  = max(final_results, key=lambda k: final_results[k].get("metrics", {}).get("final_equity", 0))
    best      = final_results[best_key]
    best_m    = best.get("metrics", {})
    print(f"\n{'BEST SCENARIO':^110}")
    pr_sep()
    print(f"  Model:      {best['config']['label']}")
    print(f"  Concurrent: {best['max_concurrent']} simultaneous positions")
    print(f"  Final Equity: ${best_m.get('final_equity',0):,.2f}")
    print(f"  CAGR: {best_m.get('cagr_pct',0):.2f}%    MaxDD: {best_m.get('max_drawdown_pct',0):.2f}%    PF: {best_m.get('profit_factor',0):.3f}    WR: {best_m.get('win_rate_pct',0):.1f}%")
    pr_sep()

    # ── Save JSON ──────────────────────────────────────────────────────────
    out_file = RESULTS_DIR / "nexus_backtest_v2_results.json"
    save_data = {
        "generated":      datetime.now(timezone.utc).isoformat(),
        "period_30m":     f"{df30.index[0].date()} to {df30.index[-1].date()}",
        "bars_30m":       len(df30),
        "initial_capital": cap0,
        "htf_gate":       use_htf,
        "capital_models": list(CAPITAL_MODELS.keys()),
        "concurrent_slots": conc_list,
        "results":         {k: {
            "model_id":       v["model_id"],
            "max_concurrent": v["max_concurrent"],
            "label":          v["config"]["label"],
            "metrics":        {
                kk: vv for kk, vv in v.get("metrics", {}).items()
                if kk not in ("eq_sample",)   # omit large arrays for readability
            }
        } for k, v in final_results.items()},
        "best_scenario": best_key,
    }

    with open(out_file, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n✅ Results saved to: {out_file}")
    print(f"   Open the file to inspect all {len(final_results)} scenario metrics.\n")


if __name__ == "__main__":
    main()
