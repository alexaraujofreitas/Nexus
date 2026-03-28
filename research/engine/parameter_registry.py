"""
research/engine/parameter_registry.py
=======================================
Central parameter definitions for PBL/SLC Research Lab.

Each parameter has:
  - settings_key  : the config.settings key read by the production model
  - default       : production-validated default value
  - dtype         : "float" | "int"
  - range_min/max : swept range (centred on default)
  - step          : coarse-sweep step size
  - description   : human-readable label
  - model         : "pbl" | "slc" | "common"
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

# ── Registry ─────────────────────────────────────────────────────────────────

ALL_PARAMS: list[ParamDef] = [
    PBL_EMA_PROX,
    PBL_SL,
    PBL_TP,
    PBL_RSI,
    SLC_ADX,
    SLC_SWING,
    SLC_SL,
    SLC_TP,
]

PARAMS_BY_KEY: dict[str, ParamDef] = {p.settings_key: p for p in ALL_PARAMS}


def default_params() -> dict[str, Any]:
    """Return dict of all parameters at their production defaults."""
    return {p.settings_key: p.default for p in ALL_PARAMS}


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
