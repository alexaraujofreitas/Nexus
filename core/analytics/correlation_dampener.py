"""
Correlation Dampener — Session 23.

Addresses the structural problem that additive confluence scoring
double-counts correlated signals.  When TrendModel and MomentumBreakout
both fire, they are both driven by price-momentum indicators (EMA, ADX, RSI).
Giving them full independent weight is false confidence.

Architecture:
- Models are grouped into correlation clusters based on their primary signal inputs.
- When N models from the same cluster fire simultaneously, each model's
  EFFECTIVE weight is multiplied by 1/sqrt(N) — diminishing returns scaling.
  - N=1: factor = 1.00 (no dampening)
  - N=2: factor = 0.71 (each contributes ~71% of normal weight)
  - N=3: factor = 0.58 (each contributes ~58% of normal weight)
- Models in different clusters or with no cluster partner receive factor = 1.0.

Cluster definitions:
  price_momentum    — trend, momentum_breakout
      Both use EMA crossovers, ADX, and RSI as primary inputs.
      If both fire they are reading the same underlying price momentum.

  mean_reversion    — mean_reversion, vwap_reversion
      Both look for price deviation from a rolling mean (SMA/EMA vs VWAP).
      Co-firing adds limited incremental information.

  microstructure    — order_book, funding_rate
      Both read real-time order-flow and market microstructure.
      Co-firing is genuine confirmation (different data sources).
      MAX_FACTOR = 0.85 (lighter dampening — more independent than price models).

  standalone        — liquidity_sweep, sentiment, rl_ensemble, orchestrator
      No intra-cluster correlation expected. factor = 1.0 always.

Configuration (config.yaml, all optional):
  correlation_dampening.enabled:   true   # master toggle
  correlation_dampening.min_factor: 0.50  # floor per model in cluster

Usage:
  from core.analytics.correlation_dampener import get_dampening_factors
  factors = get_dampening_factors(fired_model_names)
  # factors: {model_name: float} — multiply each model's weight by this factor
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from config.settings import settings as _s

logger = logging.getLogger(__name__)

# ── Cluster definitions ─────────────────────────────────────────────────────
# Group → list of model names in this correlation cluster.
# Models NOT listed here are treated as singletons (factor = 1.0).

CORRELATION_CLUSTERS: dict[str, list[str]] = {
    "price_momentum": ["trend", "momentum_breakout"],
    "mean_reversion":  ["mean_reversion", "vwap_reversion"],
    "microstructure":  ["order_book", "funding_rate"],
    # sentiment, liquidity_sweep, rl_ensemble, orchestrator → singletons
}

# Per-cluster parameters.
# microstructure gets lighter dampening because order_book and funding_rate
# use genuinely different data sources (bid/ask depth vs perpetual funding).
_CLUSTER_CONFIG: dict[str, dict] = {
    "price_momentum": {"min_factor": 0.50},
    "mean_reversion":  {"min_factor": 0.55},
    "microstructure":  {"min_factor": 0.72},
}

# Build reverse lookup: model_name → cluster_name
_MODEL_TO_CLUSTER: dict[str, str] = {}
for _cluster, _members in CORRELATION_CLUSTERS.items():
    for _m in _members:
        _MODEL_TO_CLUSTER[_m] = _cluster


def get_dampening_factors(fired_model_names: list[str]) -> dict[str, float]:
    """
    Return a dampening factor for each fired model.

    Parameters
    ----------
    fired_model_names : list of model names that produced a signal.

    Returns
    -------
    dict mapping model_name → factor in (0, 1].
    factor = 1.0 for singletons or lone cluster members.
    factor = 1/sqrt(N) for models where N >= 2 cluster-mates fired.

    The caller multiplies each model's effective weight by this factor
    BEFORE normalising across the total weight sum.
    """
    if not _s.get("correlation_dampening.enabled", True):
        return {m: 1.0 for m in fired_model_names}

    min_factor_global = float(_s.get("correlation_dampening.min_factor", 0.50))

    # Count how many fired models belong to each cluster
    cluster_counts: dict[str, int] = {}
    for m in fired_model_names:
        cluster = _MODEL_TO_CLUSTER.get(m)
        if cluster:
            cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

    factors: dict[str, float] = {}
    for m in fired_model_names:
        cluster = _MODEL_TO_CLUSTER.get(m)
        if cluster is None:
            # Standalone model — no dampening
            factors[m] = 1.0
        else:
            n = cluster_counts.get(cluster, 1)
            if n <= 1:
                factors[m] = 1.0
            else:
                raw_factor = 1.0 / math.sqrt(n)
                # Per-cluster floor (may be tighter than global floor)
                cluster_min = _CLUSTER_CONFIG.get(cluster, {}).get("min_factor", min_factor_global)
                effective_min = max(cluster_min, min_factor_global)
                factors[m] = max(effective_min, raw_factor)

    # Log only when dampening actually fires
    dampened = {m: f for m, f in factors.items() if f < 0.999}
    if dampened:
        logger.debug(
            "CorrelationDampener: co-firing dampening applied | %s",
            ", ".join(f"{m}×{f:.2f}" for m, f in dampened.items()),
        )

    return factors


def get_cluster_summary(fired_model_names: list[str]) -> dict[str, dict]:
    """
    Return a human-readable summary of cluster co-firing for diagnostics.
    Used by the rationale panel and validation dashboard.

    Returns
    -------
    dict: cluster_name → {models_fired: list, factor: float, n: int}
    """
    cluster_counts: dict[str, list[str]] = {}
    for m in fired_model_names:
        cluster = _MODEL_TO_CLUSTER.get(m)
        if cluster:
            cluster_counts.setdefault(cluster, []).append(m)

    result = {}
    for cluster, members in cluster_counts.items():
        n = len(members)
        factor = get_dampening_factors(members).get(members[0], 1.0) if n > 1 else 1.0
        result[cluster] = {
            "models_fired": members,
            "n":            n,
            "factor":       round(factor, 3),
            "dampened":     n > 1,
        }
    return result
