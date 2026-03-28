"""
research/engine/parameter_registry.py
=======================================
Central parameter definitions for the NexusTrader Research Lab.

Covers all strategy families:
  - PBL (PullbackLong)
  - SLC (SwingLowContinuation)
  - Trend (TrendModel)
  - Momentum (MomentumBreakout)

Each parameter has:
  - settings_key  : the config.settings key read by the production model
  - default       : production-validated default value
  - dtype         : "float" | "int"
  - range_min/max : swept range (centred on default)
  - step          : coarse-sweep step size
  - description   : human-readable label
  - model         : "pbl" | "slc" | "trend" | "momentum"
  - mode          : "FIXED" | "OPTIMIZE"  (user can toggle in UI)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParamDef:
    settings_key: str
    default: Any
    dtype: str           # "float" | "int"
    range_min: float
    range_max: float
    step: float
    description: str
    model: str           # "pbl" | "slc"
    mode: str = "FIXED"  # "FIXED" or "OPTIMIZE" — user toggles in UI

    def coarse_values(self) -> list:
        """Return evenly-spaced values for coarse sweep."""
        vals = []
        v = self.range_min
        while v <= self.range_max + 1e-9:
            vals.append(round(v, 6))
            v += self.step
        return vals

    def as_dict(self) -> dict:
        return {
            "settings_key": self.settings_key,
            "default":      self.default,
            "dtype":        self.dtype,
            "range_min":    self.range_min,
            "range_max":    self.range_max,
            "step":         self.step,
            "description":  self.description,
            "model":        self.model,
            "mode":         self.mode,
        }


# ── PBL parameters ────────────────────────────────────────────────────────────

PBL_EMA_PROX = ParamDef(
    settings_key="mr_pbl_slc.pullback_long.ema_prox_atr_mult",
    default=0.5,
    dtype="float",
    range_min=0.30,
    range_max=0.80,
    step=0.10,
    description="EMA50 proximity (× ATR)",
    model="pbl",
)

PBL_SL = ParamDef(
    settings_key="mr_pbl_slc.pullback_long.sl_atr_mult",
    default=2.5,
    dtype="float",
    range_min=1.5,
    range_max=4.0,
    step=0.5,
    description="PBL stop-loss (× ATR)",
    model="pbl",
)

PBL_TP = ParamDef(
    settings_key="mr_pbl_slc.pullback_long.tp_atr_mult",
    default=3.0,
    dtype="float",
    range_min=2.0,
    range_max=5.0,
    step=0.5,
    description="PBL take-profit (× ATR)",
    model="pbl",
)

PBL_RSI = ParamDef(
    settings_key="mr_pbl_slc.pullback_long.rsi_min",
    default=40.0,
    dtype="float",
    range_min=25.0,
    range_max=55.0,
    step=5.0,
    description="PBL minimum RSI",
    model="pbl",
)

# ── SLC parameters ────────────────────────────────────────────────────────────

SLC_ADX = ParamDef(
    settings_key="mr_pbl_slc.swing_low_continuation.adx_min",
    default=28.0,
    dtype="float",
    range_min=20.0,
    range_max=35.0,
    step=2.5,
    description="SLC minimum ADX",
    model="slc",
)

SLC_SWING = ParamDef(
    settings_key="mr_pbl_slc.swing_low_continuation.swing_bars",
    default=10,
    dtype="int",
    range_min=5,
    range_max=15,
    step=2,
    description="SLC swing lookback (bars)",
    model="slc",
)

SLC_SL = ParamDef(
    settings_key="mr_pbl_slc.swing_low_continuation.sl_atr_mult",
    default=2.5,
    dtype="float",
    range_min=1.5,
    range_max=4.0,
    step=0.5,
    description="SLC stop-loss (× ATR)",
    model="slc",
)

SLC_TP = ParamDef(
    settings_key="mr_pbl_slc.swing_low_continuation.tp_atr_mult",
    default=2.0,
    dtype="float",
    range_min=1.5,
    range_max=3.5,
    step=0.5,
    description="SLC take-profit (× ATR)",
    model="slc",
)

# ── TrendModel parameters ─────────────────────────────────────────────────────
# Production default for adx_min is 31.0 (config.yaml Phase 5 lever 2).
# The model reads from models.trend.adx_min; falling back to 25.0 if unset.

TREND_ADX_MIN = ParamDef(
    settings_key="models.trend.adx_min",
    default=31.0,
    dtype="float",
    range_min=20.0,
    range_max=45.0,
    step=2.5,
    description="Trend min ADX threshold",
    model="trend",
)

TREND_RSI_LONG_MIN = ParamDef(
    settings_key="models.trend.rsi_long_min",
    default=45.0,
    dtype="float",
    range_min=35.0,
    range_max=60.0,
    step=5.0,
    description="Trend long RSI floor",
    model="trend",
)

TREND_RSI_LONG_MAX = ParamDef(
    settings_key="models.trend.rsi_long_max",
    default=70.0,
    dtype="float",
    range_min=60.0,
    range_max=80.0,
    step=5.0,
    description="Trend long RSI ceiling",
    model="trend",
)

TREND_STRENGTH_BASE = ParamDef(
    settings_key="models.trend.strength_base",
    default=0.15,
    dtype="float",
    range_min=0.05,
    range_max=0.30,
    step=0.05,
    description="Trend base signal strength",
    model="trend",
)

# ── MomentumBreakout parameters ───────────────────────────────────────────────

MB_LOOKBACK = ParamDef(
    settings_key="models.momentum_breakout.lookback",
    default=20,
    dtype="int",
    range_min=10,
    range_max=35,
    step=5,
    description="Momentum breakout lookback (bars)",
    model="momentum",
)

MB_VOL_MULT = ParamDef(
    settings_key="models.momentum_breakout.vol_mult_min",
    default=1.5,
    dtype="float",
    range_min=1.0,
    range_max=2.5,
    step=0.25,
    description="Momentum min volume multiplier",
    model="momentum",
)

MB_RSI_BULLISH = ParamDef(
    settings_key="models.momentum_breakout.rsi_bullish",
    default=55.0,
    dtype="float",
    range_min=45.0,
    range_max=70.0,
    step=5.0,
    description="Momentum bullish RSI threshold",
    model="momentum",
)

MB_STRENGTH_BASE = ParamDef(
    settings_key="models.momentum_breakout.strength_base",
    default=0.35,
    dtype="float",
    range_min=0.20,
    range_max=0.50,
    step=0.05,
    description="Momentum base signal strength",
    model="momentum",
)

# ── Registry ─────────────────────────────────────────────────────────────────

#: Complete parameter list — all families
ALL_PARAMS: list[ParamDef] = [
    # PBL
    PBL_EMA_PROX,
    PBL_SL,
    PBL_TP,
    PBL_RSI,
    # SLC
    SLC_ADX,
    SLC_SWING,
    SLC_SL,
    SLC_TP,
    # Trend
    TREND_ADX_MIN,
    TREND_RSI_LONG_MIN,
    TREND_RSI_LONG_MAX,
    TREND_STRENGTH_BASE,
    # Momentum
    MB_LOOKBACK,
    MB_VOL_MULT,
    MB_RSI_BULLISH,
    MB_STRENGTH_BASE,
]

#: PBL + SLC only (for pbl_slc mode)
PBL_SLC_PARAMS: list[ParamDef] = [p for p in ALL_PARAMS if p.model in ("pbl", "slc")]

#: Trend-only params
TREND_PARAMS: list[ParamDef] = [p for p in ALL_PARAMS if p.model == "trend"]

#: Momentum-only params
MOMENTUM_PARAMS: list[ParamDef] = [p for p in ALL_PARAMS if p.model == "momentum"]

#: Look-up by settings_key
PARAMS_BY_KEY: dict[str, ParamDef] = {p.settings_key: p for p in ALL_PARAMS}

#: Params relevant per mode string (matches BacktestRunner.MODE_* constants)
PARAMS_BY_MODE: dict[str, list[ParamDef]] = {
    "pbl_slc":     PBL_SLC_PARAMS,
    "pbl":         [p for p in ALL_PARAMS if p.model == "pbl"],
    "slc":         [p for p in ALL_PARAMS if p.model == "slc"],
    "trend":       TREND_PARAMS,
    "momentum":    MOMENTUM_PARAMS,
    "full_system": ALL_PARAMS,
    "custom":      ALL_PARAMS,
}


def default_params() -> dict[str, Any]:
    """Return dict of all parameters at their production defaults."""
    return {p.settings_key: p.default for p in ALL_PARAMS}


def params_for_mode(mode: str) -> list[ParamDef]:
    """Return the subset of ParamDef relevant to the given BacktestRunner mode."""
    return PARAMS_BY_MODE.get(mode, PBL_SLC_PARAMS)


def validate_params(params: dict) -> tuple[bool, list[str]]:
    """
    Validate a parameter dict against the registry ranges.
    Returns (ok, list_of_errors).
    """
    errors = []
    for key, val in params.items():
        if key not in PARAMS_BY_KEY:
            continue  # unknown key — ignore
        p = PARAMS_BY_KEY[key]
        if val < p.range_min - 1e-9 or val > p.range_max + 1e-9:
            errors.append(
                f"{key}={val} outside range [{p.range_min}, {p.range_max}]"
            )
    return len(errors) == 0, errors
