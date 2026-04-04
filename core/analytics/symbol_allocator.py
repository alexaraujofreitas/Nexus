"""
Symbol Priority & Allocation System — Session 24 + Phase 3A.

Implements configurable, optionally dynamic per-symbol weighting so that IDSS
candidate selection favours symbols with stronger historical performance.

Phase 3A (web mode): In STATIC mode, when PostgreSQL is available,
allocation_weight is read from the Asset table on the active exchange.
This makes the web Asset Management page the SINGLE SOURCE OF TRUTH
for symbol weights. Falls back to config.yaml for desktop-only mode.

Study 4 Baseline Rankings (Backtest Study, March 2026)
    SOL/USDT — highest profit  (+1.3× weight)
    ETH/USDT — highest quality (+1.2×)
    BTC/USDT — most stable     (+1.0×, benchmark)
    BNB/USDT — mid-tier        (+0.8×)
    XRP/USDT — mid-tier        (+0.8×)

Two operating modes
    STATIC   — weights are fixed per symbol (user-configurable in Settings
               or via web Asset Management page when PostgreSQL is present)
    DYNAMIC  — weights switch between three profiles based on BTC Dominance:
                  BTC_DOMINANT  (dominance > high_threshold  %)
                  NEUTRAL       (low_threshold ≤ dominance ≤ high_threshold)
                  ALT_SEASON    (dominance < low_threshold   %)

Trade selection integration
    adjusted_score = base_score × symbol_weight
    Candidates are ranked by adjusted_score before selection in run_batch().
    This is a RANKING ONLY change — it never modifies signals, stop/target
    placement, position sizing, or any risk parameter.

Configuration (config.yaml / config/settings.py)
    symbol_allocation:
      mode: STATIC                   # or DYNAMIC
      static_weights:
        BTC/USDT: 1.0
        ETH/USDT: 1.2
        SOL/USDT: 1.3
        BNB/USDT: 0.8
        XRP/USDT: 0.8
      btc_dominance_pct: 50.0        # manual input (DYNAMIC mode)
      btc_dominance_high: 55.0       # threshold above which → BTC_DOMINANT
      btc_dominance_low:  45.0       # threshold below which → ALT_SEASON
      profiles:
        btc_dominant:                # BTC_DOMINANT profile
          BTC/USDT: 1.4
          ETH/USDT: 1.1
          SOL/USDT: 0.9
          BNB/USDT: 0.7
          XRP/USDT: 0.7
        neutral:                     # NEUTRAL profile
          BTC/USDT: 1.0
          ETH/USDT: 1.2
          SOL/USDT: 1.3
          BNB/USDT: 0.8
          XRP/USDT: 0.8
        alt_season:                  # ALT_SEASON profile
          BTC/USDT: 0.7
          ETH/USDT: 1.2
          SOL/USDT: 1.5
          BNB/USDT: 1.0
          XRP/USDT: 1.0
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from config.settings import settings as _s

logger = logging.getLogger(__name__)

# ── Phase 3A: DB weight cache ────────────────────────────────────────────────
_db_weight_cache: Optional[dict[str, float]] = None
_db_weight_cache_ts: float = 0.0
_DB_WEIGHT_CACHE_TTL = 60.0  # seconds


def _try_db_weights() -> Optional[dict[str, float]]:
    """
    Attempt to read allocation_weight from PostgreSQL (web mode).

    Returns {symbol: weight} dict if DB is available with tradable assets,
    or None if not in web mode / DB unreachable.  Cached for 60s.
    """
    global _db_weight_cache, _db_weight_cache_ts

    now = time.monotonic()
    if _db_weight_cache is not None and (now - _db_weight_cache_ts) < _DB_WEIGHT_CACHE_TTL:
        return _db_weight_cache

    try:
        from app.database import get_sync_session
        from app.models.trading import Asset, Exchange
        from sqlalchemy import select
    except ImportError:
        return None

    try:
        with get_sync_session() as session:
            active_ex = session.execute(
                select(Exchange).where(Exchange.is_active.is_(True))
            ).scalar_one_or_none()
            if active_ex is None:
                return None

            rows = session.execute(
                select(Asset.symbol, Asset.allocation_weight)
                .where(
                    Asset.exchange_id == active_ex.id,
                    Asset.is_tradable.is_(True),
                )
            ).all()

            if not rows:
                return None

            weights = {sym.upper().strip(): float(w) for sym, w in rows}
            _db_weight_cache = weights
            _db_weight_cache_ts = now
            logger.debug(
                "SymbolAllocator: DB weights loaded → %d symbols", len(weights),
            )
            return weights
    except Exception as exc:
        logger.warning(
            "SymbolAllocator: PostgreSQL weight query failed, using config: %s", exc,
        )
        return None

# ── Regime identifiers ────────────────────────────────────────────────────────
REGIME_BTC_DOMINANT = "BTC_DOMINANT"
REGIME_NEUTRAL      = "NEUTRAL"
REGIME_ALT_SEASON   = "ALT_SEASON"

# Profile name → settings sub-key
_PROFILE_KEY: dict[str, str] = {
    REGIME_BTC_DOMINANT: "btc_dominant",
    REGIME_NEUTRAL:      "neutral",
    REGIME_ALT_SEASON:   "alt_season",
}

# Fallback weights when a symbol is not explicitly configured
_DEFAULT_WEIGHT = 1.0

# Hard bounds so a user typo cannot produce extreme leverage or blacklisting
_MIN_WEIGHT = 0.10
_MAX_WEIGHT = 3.00


def _clamp_weight(w: float) -> float:
    return max(_MIN_WEIGHT, min(_MAX_WEIGHT, float(w)))


class SymbolAllocator:
    """
    Stateless helper that reads from ``config.settings`` on every call.
    No caching — settings reads are fast dict lookups and this ensures
    real-time responsiveness to user config changes without restart.

    Public API
    ----------
    get_weight(symbol)          → float  (symbol_weight)
    get_adjusted_score(c)       → float  (candidate["score"] × weight)
    rank_candidates(candidates) → list[dict] sorted descending by adjusted_score
    get_regime()                → str    (active dominance regime, DYNAMIC mode only)
    get_status()                → dict   (diagnostic snapshot)
    """

    # ── public interface ──────────────────────────────────────────────────────

    def get_weight(self, symbol: str) -> float:
        """
        Return the allocation weight for *symbol*.

        In STATIC mode:
          1. (Phase 3A) Try PostgreSQL Asset.allocation_weight first
          2. Fall back to config.yaml static_weights
        In DYNAMIC mode, selects the active profile based on BTC dominance
        and returns the profile weight (DB not used — profiles are config-driven).
        """
        mode = _s.get("symbol_allocation.mode", "STATIC").upper().strip()

        if mode == "DYNAMIC":
            regime  = self._active_regime()
            profile = _PROFILE_KEY[regime]
            raw     = _s.get(f"symbol_allocation.profiles.{profile}.{symbol}", _DEFAULT_WEIGHT)
        else:
            # STATIC — Phase 3A: DB-authoritative path
            db_weights = _try_db_weights()
            if db_weights is not None:
                raw = db_weights.get(symbol.upper().strip(), _DEFAULT_WEIGHT)
            else:
                # Desktop fallback: config.yaml
                raw = _s.get(f"symbol_allocation.static_weights.{symbol}", _DEFAULT_WEIGHT)

        weight = _clamp_weight(float(raw))
        return weight

    def get_adjusted_score(self, candidate: dict) -> float:
        """
        Compute ``adjusted_score = base_score × symbol_weight``.

        The original ``candidate["score"]`` is NOT mutated.
        Returns the adjusted float value.
        """
        symbol     = candidate.get("symbol", "")
        base_score = float(candidate.get("score", 0.0))
        weight     = self.get_weight(symbol)
        return base_score * weight

    def rank_candidates(self, candidates: list[dict]) -> list[dict]:
        """
        Return *candidates* sorted descending by ``adjusted_score``.

        Stamps ``adjusted_score`` and ``symbol_weight`` keys onto each
        candidate dict (in-place) for logging and diagnostics.
        The original ``score`` key is preserved unchanged.
        """
        if not candidates:
            return candidates

        for c in candidates:
            w  = self.get_weight(c.get("symbol", ""))
            adj = float(c.get("score", 0.0)) * w
            c["symbol_weight"]   = w
            c["adjusted_score"]  = adj

        ranked = sorted(candidates, key=lambda c: c["adjusted_score"], reverse=True)

        if len(ranked) > 1:
            mode   = _s.get("symbol_allocation.mode", "STATIC").upper().strip()
            regime = self.get_regime()
            logger.info(
                "SymbolAllocator: ranked %d candidates | mode=%s regime=%s | "
                "order=%s",
                len(ranked),
                mode,
                regime,
                " > ".join(
                    f"{c.get('symbol','?')} "
                    f"(base={c.get('score', 0):.3f} ×{c.get('symbol_weight', 1):.2f}"
                    f"={c.get('adjusted_score', 0):.3f})"
                    for c in ranked
                ),
            )
        return ranked

    def get_regime(self) -> str:
        """
        Return the active dominance regime string.

        Always returns one of the three REGIME_* constants.
        In STATIC mode returns NEUTRAL (weights come from static_weights,
        not a profile, but a regime label is still useful for logs).
        """
        mode = _s.get("symbol_allocation.mode", "STATIC").upper().strip()
        if mode != "DYNAMIC":
            return REGIME_NEUTRAL
        return self._active_regime()

    def get_status(self) -> dict:
        """Return a diagnostic snapshot dict for the rationale panel / logs."""
        mode   = _s.get("symbol_allocation.mode", "STATIC").upper().strip()
        regime = self.get_regime()
        dom    = float(_s.get("symbol_allocation.btc_dominance_pct", 50.0))
        return {
            "mode":            mode,
            "active_regime":   regime,
            "btc_dominance":   dom,
            "dom_high_thresh": float(_s.get("symbol_allocation.btc_dominance_high", 55.0)),
            "dom_low_thresh":  float(_s.get("symbol_allocation.btc_dominance_low", 45.0)),
        }

    # ── private helpers ───────────────────────────────────────────────────────

    def _active_regime(self) -> str:
        """
        Classify the current BTC dominance reading into a regime.
        Reads from ``symbol_allocation.btc_dominance_pct`` (user-set).
        """
        dom      = float(_s.get("symbol_allocation.btc_dominance_pct", 50.0))
        high_thr = float(_s.get("symbol_allocation.btc_dominance_high", 55.0))
        low_thr  = float(_s.get("symbol_allocation.btc_dominance_low",  45.0))

        if dom > high_thr:
            regime = REGIME_BTC_DOMINANT
        elif dom < low_thr:
            regime = REGIME_ALT_SEASON
        else:
            regime = REGIME_NEUTRAL

        logger.debug(
            "SymbolAllocator: BTC dominance=%.1f%% high=%.1f%% low=%.1f%% → regime=%s",
            dom, high_thr, low_thr, regime,
        )
        return regime


# ── Module-level singleton ────────────────────────────────────────────────────

_allocator_instance: Optional[SymbolAllocator] = None


def get_allocator() -> SymbolAllocator:
    """Return the module-level SymbolAllocator singleton."""
    global _allocator_instance
    if _allocator_instance is None:
        _allocator_instance = SymbolAllocator()
    return _allocator_instance
