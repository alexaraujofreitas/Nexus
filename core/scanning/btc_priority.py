# ============================================================
# NEXUS TRADER — BTC-First Architecture Module
#
# Implements BTC prioritization across the entire signal pipeline:
#
#   1. Symbol priority scoring — BTC always gets highest weight
#   2. Regime-gated universe filter — BTC regime gates alt entries
#   3. Position sizing multiplier — BTC gets larger size allocation
#   4. Signal confidence boost — BTC signals get confidence bonus
#   5. Alt-entry gate — require BTC regime confirmation before alts
#
# Design principles:
#   - BTC is always watched/scanned regardless of watchlist
#   - BTC/USDT regime is the primary market regime proxy
#   - Alt signals require BTC regime alignment (configurable)
#   - BTC position sizing uses 1.5x multiplier vs alts
#
# Publishes: Topics.BTC_PRIORITY_UPDATE on regime changes
# ============================================================
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# BTC must always be in the trading universe
BTC_SYMBOL        = "BTC/USDT"
BTC_COINGECKO_ID  = "bitcoin"
BTC_BINANCE_SYM   = "BTCUSDT"

# Multipliers relative to base allocation
BTC_SIZE_MULTIPLIER   = 1.5   # BTC gets 50% more capital
BTC_CONFIDENCE_BOOST  = 0.05  # +5% confidence on all BTC signals

# Alt-entry gate modes
ALT_GATE_DISABLED    = "disabled"     # alts always allowed
ALT_GATE_SOFT        = "soft"         # reduce alt size in bear regime
ALT_GATE_HARD        = "hard"         # block alt entries in bear regime

# Regimes that allow alt entries in hard mode
_ALT_ALLOWED_REGIMES = {
    "TRENDING_UP", "RECOVERY", "RANGING", "BREAKOUT",
    "ACCUMULATION", "MARKUP",
}


class BTCPriorityFilter:
    """
    Applies BTC-first prioritization rules to the signal pipeline.

    Used by:
      - MarketScanner: to prioritize BTC scan + gate alt entries
      - PositionSizer: to apply BTC size multiplier
      - OrchestatorEngine: to apply confidence boost on BTC signals
      - RegimePage: to publish BTC regime as primary market regime
    """

    def __init__(self):
        self._lock              = threading.RLock()
        self._btc_regime        = "UNKNOWN"
        self._btc_confidence    = 0.0
        self._alt_gate_mode     = ALT_GATE_SOFT
        self._last_update: Optional[datetime] = None
        self._btc_priority_score = 1.0   # dynamic [0, 1] based on regime quality

        # Subscribe to regime changes
        try:
            from core.event_bus import bus, Topics
            bus.subscribe(Topics.REGIME_CHANGED, self._on_regime_changed)
        except Exception as exc:
            logger.debug("BTCPriorityFilter: event bus subscribe failed: %s", exc)

    # ── Configuration ─────────────────────────────────────────

    def set_alt_gate_mode(self, mode: str) -> None:
        """Set alt entry gate mode: 'disabled', 'soft', or 'hard'."""
        if mode not in (ALT_GATE_DISABLED, ALT_GATE_SOFT, ALT_GATE_HARD):
            logger.warning("BTCPriorityFilter: unknown gate mode '%s'", mode)
            return
        with self._lock:
            self._alt_gate_mode = mode
        logger.info("BTCPriorityFilter: alt gate mode → %s", mode)

    # ── Priority scoring ──────────────────────────────────────

    def get_symbol_priority(self, symbol: str) -> float:
        """
        Return priority score [0, 1] for *symbol*.
        BTC always returns 1.0. Alts are scored based on BTC regime.
        """
        base = symbol.split("/")[0].upper()
        if base == "BTC":
            return 1.0
        with self._lock:
            regime = self._btc_regime
            conf   = self._btc_confidence
        return self._alt_priority(regime, conf)

    def _alt_priority(self, btc_regime: str, btc_confidence: float) -> float:
        """Score alt entry attractiveness given BTC regime."""
        if btc_regime in ("TRENDING_UP", "MARKUP", "RECOVERY"):
            return 0.8 + btc_confidence * 0.2
        elif btc_regime in ("RANGING", "ACCUMULATION", "BREAKOUT"):
            return 0.6 + btc_confidence * 0.2
        elif btc_regime in ("TRENDING_DOWN", "DISTRIBUTION", "MARKDOWN"):
            return 0.3
        elif btc_regime in ("CRASH", "CAPITULATION"):
            return 0.1
        return 0.5   # UNKNOWN

    # ── Size multiplier ───────────────────────────────────────

    def get_size_multiplier(self, symbol: str) -> float:
        """Return position size multiplier for *symbol*."""
        base = symbol.split("/")[0].upper()
        if base == "BTC":
            return BTC_SIZE_MULTIPLIER
        return 1.0

    # ── Confidence adjustment ─────────────────────────────────

    def adjust_confidence(self, symbol: str, confidence: float) -> float:
        """Apply BTC confidence boost if applicable."""
        base = symbol.split("/")[0].upper()
        if base == "BTC":
            return min(1.0, confidence + BTC_CONFIDENCE_BOOST)
        return confidence

    # ── Alt-entry gate ────────────────────────────────────────

    def is_alt_entry_allowed(self, symbol: str, signal_strength: float = 0.5) -> tuple[bool, str]:
        """
        Check whether an alt-coin entry is permitted.

        Returns (allowed: bool, reason: str).
        BTC itself is always allowed.
        """
        base = symbol.split("/")[0].upper()
        if base == "BTC":
            return True, "BTC always allowed"

        with self._lock:
            mode   = self._alt_gate_mode
            regime = self._btc_regime
            conf   = self._btc_confidence

        if mode == ALT_GATE_DISABLED:
            return True, "alt gate disabled"

        if mode == ALT_GATE_HARD:
            if regime not in _ALT_ALLOWED_REGIMES:
                return False, f"hard gate: BTC regime={regime} blocks alts"
            if conf < 0.4:
                return False, f"hard gate: BTC confidence={conf:.2f} too low"

        if mode == ALT_GATE_SOFT:
            # Allow but caller should reduce size
            if regime in ("CRASH", "CAPITULATION"):
                return False, f"soft gate: BTC in {regime} — blocking alts"

        return True, f"alt allowed (BTC regime={regime})"

    def get_alt_size_reduction(self, symbol: str) -> float:
        """
        Return size reduction factor [0, 1] for alts based on BTC regime.
        1.0 = no reduction; 0.5 = halve the size.
        Only used in SOFT gate mode.
        """
        base = symbol.split("/")[0].upper()
        if base == "BTC":
            return 1.0
        with self._lock:
            regime = self._btc_regime
        if regime in ("CRASH", "CAPITULATION"):
            return 0.2
        if regime in ("TRENDING_DOWN", "DISTRIBUTION", "MARKDOWN"):
            return 0.5
        if regime in ("RANGING", "ACCUMULATION"):
            return 0.75
        return 1.0

    # ── State ─────────────────────────────────────────────────

    def get_btc_regime(self) -> str:
        with self._lock:
            return self._btc_regime

    def get_status(self) -> dict:
        with self._lock:
            return {
                "btc_regime":       self._btc_regime,
                "btc_confidence":   round(self._btc_confidence, 3),
                "alt_gate_mode":    self._alt_gate_mode,
                "priority_score":   round(self._btc_priority_score, 3),
                "last_update":      self._last_update.isoformat() if self._last_update else None,
            }

    # ── Event handler ─────────────────────────────────────────

    def _on_regime_changed(self, event) -> None:
        data = event.data if hasattr(event, "data") and isinstance(event.data, dict) else {}
        regime = data.get("new_regime", "UNKNOWN")
        conf   = float(data.get("confidence", 0.5))
        with self._lock:
            self._btc_regime     = regime
            self._btc_confidence = conf
            self._last_update    = datetime.now(timezone.utc)
            self._btc_priority_score = self._alt_priority(regime, conf)
        logger.info(
            "BTCPriorityFilter: regime=%s conf=%.2f priority=%.2f",
            regime, conf, self._btc_priority_score,
        )
        try:
            from core.event_bus import bus, Topics
            bus.publish(Topics.BTC_PRIORITY_UPDATE, self.get_status(), source="btc_priority")
        except Exception:
            pass


# ── Module-level singleton ────────────────────────────────────
_btc_priority_filter: Optional[BTCPriorityFilter] = None
_filter_lock = threading.Lock()


def get_btc_priority_filter() -> BTCPriorityFilter:
    """Return global BTCPriorityFilter singleton."""
    global _btc_priority_filter
    if _btc_priority_filter is None:
        with _filter_lock:
            if _btc_priority_filter is None:
                _btc_priority_filter = BTCPriorityFilter()
    return _btc_priority_filter


def ensure_btc_in_universe(symbols: list[str]) -> list[str]:
    """
    Ensure BTC/USDT is always present in the universe.
    Inserts BTC at the front if not already present.
    """
    if BTC_SYMBOL not in symbols:
        return [BTC_SYMBOL] + list(symbols)
    # Move BTC to front
    rest = [s for s in symbols if s != BTC_SYMBOL]
    return [BTC_SYMBOL] + rest
