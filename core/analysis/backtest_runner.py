# ============================================================
# NEXUS TRADER — Backtest Runner (Phase 3)
#
# Bridges the tuning-proposal pipeline into the real IDSSBacktester.
# Runs baseline vs candidate comparison using real historical
# exchange data only (data/validation/ parquets).
#
# Pipeline:
#   proposal → build_backtest_spec() → load_parquet_data()
#            → run_baseline() → run_candidate() → compare()
#            → evaluate_proposal_vs_baseline() → persist_result()
# ============================================================
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_VAL_DIR = Path(__file__).parent.parent.parent / "data" / "validation"


class BacktestRunnerError(RuntimeError):
    """Raised when a backtest run cannot be completed."""
    pass


def _load_parquet(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Load real historical OHLCV data from the validation parquet store.
    Raises BacktestRunnerError if the parquet does not exist.
    """
    slug = symbol.replace("/", "") if "/" in symbol else symbol
    if not slug.endswith("USDT"):
        slug = slug + "USDT"
    path = _VAL_DIR / f"{slug}_{timeframe}.parquet"
    if not path.exists():
        raise BacktestRunnerError(
            f"No validation parquet for {symbol}/{timeframe} — "
            f"expected {path}. Run scripts/fetch_historical_data_v2.py first."
        )
    df = pd.read_parquet(path)
    logger.info("BacktestRunner: loaded %d bars from %s", len(df), path.name)
    return df


def _run_idss(df: pd.DataFrame, symbol: str, timeframe: str,
              min_confluence_score: Optional[float] = None,
              disabled_models: Optional[list[str]] = None,
              initial_capital: float = 10_000.0) -> dict:
    """
    Run the IDSS backtester on df with optional parameter overrides.
    Returns the raw result dict from IDSSBacktester.run().
    """
    from core.features.indicator_library import calculate_all
    from core.backtesting.idss_backtester import IDSSBacktester

    df_ind = calculate_all(df.copy())

    kwargs = {}
    if min_confluence_score is not None:
        kwargs["min_confluence_score"] = min_confluence_score

    backtester = IDSSBacktester(**kwargs)
    result = backtester.run(
        df=df_ind,
        symbol=symbol,
        timeframe=timeframe,
        initial_capital=initial_capital,
        fee_pct=0.10,
        slippage_pct=0.05,
        spread_pct=0.05,
    )
    return result


def _extract_kpis(result: dict) -> dict:
    """Extract KPI summary from IDSSBacktester result dict."""
    trades = result.get("trades", [])
    metrics = result.get("metrics") or {}
    wins = sum(1 for t in trades if float(t.get("pnl_pct", 0)) > 0)
    n = len(trades)
    return {
        "trade_count":   n,
        "win_rate":      round(wins / n * 100, 2) if n > 0 else 0.0,
        "profit_factor": float(metrics.get("profit_factor", 0.0)),
        "max_drawdown":  float(metrics.get("max_drawdown", 0.0)),
        "sharpe":        float(metrics.get("sharpe", 0.0)),
        "total_pnl_pct": float(metrics.get("total_return_pct", 0.0)),
    }


def run_proposal_backtest(
    proposal: dict,
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    initial_capital: float = 10_000.0,
    dry_run: bool = False,
) -> dict:
    """
    Run a full baseline vs candidate backtest for a tuning proposal.

    Parameters
    ----------
    proposal : dict
        A StrategyTuningProposal dict (from load_pending_proposals()).
    symbol : str
        Symbol to backtest on (e.g. "BTCUSDT").
    timeframe : str
        Timeframe to backtest on (e.g. "1h").
    initial_capital : float
        Starting equity.
    dry_run : bool
        If True, skips actual backtest and returns a synthetic pass result
        for testing pipelines. NEVER used in production.

    Returns
    -------
    dict with keys:
        proposal_id, symbol, timeframe, baseline_kpis, candidate_kpis,
        gating_result (APPROVE/REJECT/MANUAL_REVIEW), pf_delta_pct,
        wr_delta_pp, auto_promotable, ran_at, error (if any)
    """
    proposal_id = proposal.get("proposal_id", "unknown")
    ran_at = datetime.now(timezone.utc).isoformat()

    if dry_run:
        logger.warning(
            "BacktestRunner: DRY RUN for proposal %s — results are synthetic",
            proposal_id,
        )
        synthetic = {
            "proposal_id": proposal_id,
            "symbol": symbol,
            "timeframe": timeframe,
            "baseline_kpis": {"trade_count": 25, "win_rate": 55.0,
                              "profit_factor": 1.45, "max_drawdown": 8.0, "sharpe": 0.9},
            "candidate_kpis": {"trade_count": 22, "win_rate": 59.0,
                               "profit_factor": 1.52, "max_drawdown": 7.2, "sharpe": 1.1},
            "gating_result": "APPROVE",
            "pf_delta_pct": 4.83,
            "wr_delta_pp": 4.0,
            "auto_promotable": False,
            "ran_at": ran_at,
            "dry_run": True,
        }
        return synthetic

    try:
        logger.info(
            "BacktestRunner: starting backtest for proposal %s | %s/%s",
            proposal_id, symbol, timeframe,
        )
        t0 = time.time()

        # Load real historical data
        df = _load_parquet(symbol, timeframe)

        # ── Baseline run ──────────────────────────────────────
        logger.info("BacktestRunner: running baseline for %s", proposal_id)
        baseline_result = _run_idss(df, symbol, timeframe,
                                    initial_capital=initial_capital)
        baseline_kpis = _extract_kpis(baseline_result)
        logger.info("BacktestRunner: baseline complete — %d trades, PF=%.2f",
                    baseline_kpis["trade_count"], baseline_kpis["profit_factor"])

        # ── Candidate run (with proposed parameter change) ────
        candidate_override = _build_candidate_override(proposal)
        logger.info(
            "BacktestRunner: running candidate for %s | overrides=%s",
            proposal_id, candidate_override,
        )
        candidate_result = _run_idss(
            df, symbol, timeframe,
            initial_capital=initial_capital,
            **candidate_override,
        )
        candidate_kpis = _extract_kpis(candidate_result)
        logger.info("BacktestRunner: candidate complete — %d trades, PF=%.2f",
                    candidate_kpis["trade_count"], candidate_kpis["profit_factor"])

        elapsed = round(time.time() - t0, 1)

        # ── Gating decision ───────────────────────────────────
        from core.analysis.backtest_gating import evaluate_proposal_vs_baseline
        backtest_results = {
            "baseline_pf":    baseline_kpis["profit_factor"],
            "candidate_pf":   candidate_kpis["profit_factor"],
            "baseline_wr":    baseline_kpis["win_rate"],
            "candidate_wr":   candidate_kpis["win_rate"],
            "trade_count":    candidate_kpis["trade_count"],
        }
        gating = evaluate_proposal_vs_baseline(proposal, backtest_results)

        run_record = {
            "proposal_id":    proposal_id,
            "symbol":         symbol,
            "timeframe":      timeframe,
            "baseline_kpis":  baseline_kpis,
            "candidate_kpis": candidate_kpis,
            "gating_result":  gating["decision"],
            "pf_delta_pct":   gating.get("pf_delta_pct", 0.0),
            "wr_delta_pp":    gating.get("wr_delta_pp", 0.0),
            "auto_promotable": gating.get("auto_promotable", False),
            "ran_at":         ran_at,
            "elapsed_s":      elapsed,
            "dry_run":        False,
        }

        # ── Persist gating result back onto proposal ──────────
        try:
            from core.analysis.backtest_gating import update_proposal_after_evaluation
            update_proposal_after_evaluation(proposal_id, gating, backtest_results)
            logger.info(
                "BacktestRunner: persisted gating result %s for proposal %s",
                gating["decision"], proposal_id,
            )
        except Exception as _pe:
            logger.warning(
                "BacktestRunner: could not persist gating result for %s: %s",
                proposal_id, _pe,
            )

        # ── Metrics ───────────────────────────────────────────
        try:
            from core.analysis.analysis_metrics import log_backtest_result
            log_backtest_result(proposal_id, True, gating.get("pf_delta_pct", 0.0))
        except Exception:
            pass

        logger.info(
            "BacktestRunner: COMPLETE | proposal=%s decision=%s pf_delta=%.2f%% elapsed=%ss",
            proposal_id, gating["decision"], gating.get("pf_delta_pct", 0.0), elapsed,
        )
        return run_record

    except BacktestRunnerError as e:
        logger.error("BacktestRunner: data error for %s: %s", proposal_id, e)
        try:
            from core.analysis.analysis_metrics import inc, C_BACKTEST_RUN_ERROR
            inc(C_BACKTEST_RUN_ERROR)
        except Exception:
            pass
        return {
            "proposal_id": proposal_id, "symbol": symbol, "timeframe": timeframe,
            "gating_result": "ERROR", "error": str(e), "ran_at": ran_at,
        }
    except Exception as e:
        logger.error(
            "BacktestRunner: unexpected error for proposal %s: %s",
            proposal_id, e, exc_info=True,
        )
        try:
            from core.analysis.analysis_metrics import inc, C_BACKTEST_RUN_ERROR
            inc(C_BACKTEST_RUN_ERROR)
        except Exception:
            pass
        return {
            "proposal_id": proposal_id, "symbol": symbol, "timeframe": timeframe,
            "gating_result": "ERROR", "error": str(e), "ran_at": ran_at,
        }


def _build_candidate_override(proposal: dict) -> dict:
    """
    Build keyword arguments for IDSSBacktester corresponding to the
    tuning proposal's proposed parameter change.
    Returns empty dict if the parameter is not directly mappable.
    """
    param    = proposal.get("tuning_parameter", "")
    direction = proposal.get("tuning_direction", "")

    overrides: dict = {}

    if param == "min_confluence_score":
        # Read current value from config and apply direction
        try:
            from config.settings import settings
            current = float(settings.get("idss.min_confluence_score", 0.45))
            delta = 0.04 if direction == "increase" else -0.04
            overrides["min_confluence_score"] = round(current + delta, 3)
        except Exception:
            overrides["min_confluence_score"] = 0.50 if direction == "increase" else 0.40

    elif param == "regime_affinity_weight":
        # Regime weight is internal to ConfluenceScorer — we proxy via confluence threshold
        # A tighter regime filter effectively raises the effective confluence requirement
        try:
            from config.settings import settings
            current = float(settings.get("idss.min_confluence_score", 0.45))
            overrides["min_confluence_score"] = round(current + 0.03, 3)
        except Exception:
            overrides["min_confluence_score"] = 0.48

    # Other parameters that don't map directly to IDSSBacktester kwargs
    # are logged and the baseline config is used (gating still runs)
    if not overrides:
        logger.info(
            "BacktestRunner: parameter '%s' has no direct backtester mapping — "
            "running candidate at baseline config for gating comparison", param,
        )

    return overrides


def run_all_pending_proposals(
    symbol: str = "BTCUSDT",
    timeframe: str = "1h",
    max_proposals: int = 10,
) -> list[dict]:
    """
    Load all pending proposals and run backtests for each.
    Returns list of run_record dicts.
    """
    try:
        from core.analysis.backtest_gating import load_pending_proposals
    except ImportError:
        from core.analysis.tuning_proposal_generator import load_pending_proposals

    proposals = load_pending_proposals()
    if not proposals:
        logger.info("BacktestRunner: no pending proposals to process")
        return []

    proposals = proposals[:max_proposals]
    logger.info("BacktestRunner: processing %d pending proposals", len(proposals))

    results = []
    for p in proposals:
        record = run_proposal_backtest(p, symbol=symbol, timeframe=timeframe)
        results.append(record)

    return results
