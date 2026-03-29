# ============================================================
# NEXUS TRADER — Demo Mode Startup Validation Log
#
# Called once at system startup when demo_mode.locked=True.
# Prints a structured banner confirming:
#   • active models and parameters
#   • disabled models
#   • DEMO MODE ACTIVE status
#   • parameter lock version
#
# This module has zero side effects beyond logging.
# ============================================================
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# ── Expected locked parameter values (must match Session 50 research) ────
_EXPECTED_PBL_PARAMS = {
    "sl_atr_mult":       3.0,
    "tp_atr_mult":       4.0,
    "ema_prox_atr_mult": 0.4,
    "rsi_min":           45.0,
    "wick_strength":     1.5,
}
_EXPECTED_DISABLED = {
    "mean_reversion", "liquidity_sweep", "trend", "donchian_breakout", "momentum_breakout",
}


def run_demo_startup_validation() -> bool:
    """
    Validate config against expected demo-mode values.
    Logs a structured banner.  Returns True if all checks pass.

    Must be called after config.settings is loaded.
    """
    try:
        from config.settings import settings
    except Exception as exc:
        logger.error("DemoStartupLog: cannot import settings — %s", exc)
        return False

    sep = "=" * 72
    logger.info(sep)
    logger.info("  NEXUS TRADER — DEMO MODE ACTIVE")
    logger.info(sep)

    locked       = settings.get("demo_mode.locked", False)
    lock_version = settings.get("demo_mode.parameter_lock_version", "unknown")
    note         = settings.get("demo_mode.note", "")

    logger.info("  Demo Lock Status  : %s", "LOCKED" if locked else "UNLOCKED ⚠️")
    logger.info("  Lock Version      : %s", lock_version)
    if note:
        logger.info("  Note              : %s", note)

    # ── Active models ──────────────────────────────────────────────────
    disabled_in_cfg = set(settings.get("disabled_models", []))
    mr_enabled      = bool(settings.get("mr_pbl_slc.enabled", False))

    logger.info("")
    logger.info("  ACTIVE MODELS:")
    if mr_enabled:
        logger.info("    ✓ PullbackLong (PBL)           — 30m bull_trend regime")
        logger.info("    ✓ SwingLowContinuation (SLC)   — 1h  bear_trend regime")
    else:
        logger.warning("    ✗ PBL + SLC DISABLED (mr_pbl_slc.enabled=false) — check config!")
    logger.info("    ✓ FundingRateModel             — context enrichment (low weight)")
    logger.info("    ✓ SentimentModel               — context enrichment (low weight)")

    logger.info("")
    logger.info("  DISABLED MODELS:")
    for m in sorted(disabled_in_cfg):
        logger.info("    ✗ %s", m)

    # ── PBL parameters ─────────────────────────────────────────────────
    logger.info("")
    logger.info("  PBL PARAMETERS (Session 50 approved):")
    all_params_ok = True
    param_keys = [
        "sl_atr_mult", "tp_atr_mult", "ema_prox_atr_mult", "rsi_min", "wick_strength",
    ]
    for k in param_keys:
        cfg_val  = settings.get(f"mr_pbl_slc.pullback_long.{k}", None)
        exp_val  = _EXPECTED_PBL_PARAMS.get(k)
        match    = "✓" if cfg_val == exp_val else "✗ MISMATCH"
        if cfg_val != exp_val:
            all_params_ok = False
        logger.info("    %s  %-22s = %s  (expected %s)", match, k, cfg_val, exp_val)

    # ── Disabled-models check ──────────────────────────────────────────
    logger.info("")
    logger.info("  DISABLED MODEL CHECK:")
    disabled_ok = True
    for m in sorted(_EXPECTED_DISABLED):
        present = m in disabled_in_cfg
        mark    = "✓" if present else "✗ MISSING"
        if not present:
            disabled_ok = False
        logger.info("    %s  %s", mark, m)

    # ── Execution mode (Session 51: BACKTEST_PARITY_WITH_AI) ─────────
    logger.info("")
    logger.info("  EXECUTION MODE:")
    _parity_on     = bool(settings.get("execution_mode.backtest_parity", False))
    _ai_filter     = bool(settings.get("execution_mode.ai_filter_only", True))
    _pos_frac      = settings.get("execution_mode.parity_pos_frac", 0.35)
    _max_heat      = settings.get("execution_mode.parity_max_heat", 0.80)
    _max_positions = settings.get("execution_mode.parity_max_positions", 10)
    _max_per_asset = settings.get("execution_mode.parity_max_per_asset", 3)
    if _parity_on:
        logger.info("    ✓  backtest_parity     = True  (BACKTEST_PARITY_WITH_AI)")
        logger.info("    ✓  sizing              = pos_frac (%.0f%% equity)", _pos_frac * 100)
        logger.info("    ✓  exit logic          = static SL/TP only (no partial/BE/trailing)")
        logger.info("    ✓  max_heat            = %.0f%%", _max_heat * 100)
        logger.info("    ✓  max_positions       = %s", _max_positions)
        logger.info("    ✓  max_per_asset       = %s", _max_per_asset)
        logger.info("    ✓  ai_filter_only      = %s", _ai_filter)
    else:
        logger.warning("    ✗  backtest_parity     = False  (STANDARD risk-based mode)")
        logger.info("    …  risk_pct_per_trade  = %s%%", settings.get("risk_engine.risk_pct_per_trade", "?"))
        logger.info("    …  exit.mode           = %s", settings.get("exit.mode", "?"))

    # ── Global settings ────────────────────────────────────────────────
    logger.info("")
    logger.info("  GLOBAL SETTINGS:")
    logger.info(
        "    timeframe              = %s", settings.get("data.default_timeframe", "?"),
    )
    logger.info(
        "    min_confluence_score   = %s", settings.get("idss.min_confluence_score", "?"),
    )
    logger.info(
        "    multi_tf.confirmation  = %s", settings.get("multi_tf.confirmation_required", "?"),
    )
    logger.info(
        "    scanner.auto_execute   = %s", settings.get("scanner.auto_execute", "?"),
    )
    logger.info(
        "    websocket_enabled      = %s", settings.get("data.websocket_enabled", "?"),
    )

    # ── Final verdict ──────────────────────────────────────────────────
    all_ok = locked and all_params_ok and disabled_ok and mr_enabled and _parity_on
    logger.info("")
    if all_ok:
        logger.info("  STATUS: ✅  ALL CHECKS PASSED — DEMO TRADING READY (PARITY MODE)")
    else:
        issues = []
        if not locked:
            issues.append("demo_mode.locked is False")
        if not all_params_ok:
            issues.append("PBL param mismatch")
        if not disabled_ok:
            issues.append("disabled_models incomplete")
        if not mr_enabled:
            issues.append("mr_pbl_slc.enabled=false")
        if not _parity_on:
            issues.append("execution_mode.backtest_parity=false (SHOULD BE true)")
        logger.critical(
            "  STATUS: ❌  DEMO STARTUP FAILED — issues: %s", "; ".join(issues),
        )
    logger.info(sep)
    return all_ok
