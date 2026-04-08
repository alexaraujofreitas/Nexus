# ============================================================
# NEXUS TRADER — Position Sizer
#
# PRODUCTION MODE (default): Risk-Based Sizing
#   risk_usdt  = risk_pct% × capital
#   qty        = risk_usdt / stop_distance
#   size_usdt  = qty × entry_price
#   Capped at 25% of capital, floored at min_size_usdt.
#   Controlled by config: risk_engine.sizing_mode = "risk_based"
#
# LEGACY MODE: Quarter-Kelly Criterion (disabled in production)
#   base_kelly = kelly_fraction * available_capital_usdt
#   vol_scalar = target_atr_pct / current_atr_pct (clamped [0.2, 3.0])
#   size = base_kelly * vol_scalar * regime_mult * score_mult * drawdown_scalar
#
# Study 4 validated risk-based sizing across 4 years:
#   Conservative 0.5%/3%: E[R]=0.540R, PF=2.18, MaxDD=-3.9%
#   Moderate    0.75%/4%: E[R]=0.540R, PF=2.18, MaxDD=-3.9%
#   Aggressive   1.0%/5%: E[R]=0.540R, PF=2.18, MaxDD=-3.9%
# ============================================================
from __future__ import annotations

import logging
from typing import Optional
logger = logging.getLogger(__name__)

# Module-level import to avoid import lock contention at runtime.
# The crash_detector module is imported ONCE at module load time,
# not on every calculate() call.  This prevents the scanner thread
# from blocking on Python's import lock when FinBERT/HuggingFace
# is loading on another thread.
_crash_detector_ref = None

def _get_crash_detector_safe():
    """Get crash detector singleton without holding import lock in hot path."""
    global _crash_detector_ref
    if _crash_detector_ref is None:
        try:
            from core.risk.crash_detector import get_crash_detector
            _crash_detector_ref = get_crash_detector()
        except Exception:
            return None
    return _crash_detector_ref


class PositionSizer:
    """
    Kelly Criterion-based position sizing with regime and drawdown adjustments.

    Safe for trading by using half-Kelly and multiple risk dampeners.
    """

    REGIME_RISK_MULTIPLIERS: dict[str, float] = {
        "bull_trend":              1.0,
        "bear_trend":              0.7,
        "ranging":                 0.8,
        "volatility_expansion":    0.6,
        "volatility_compression":  0.5,
        "accumulation":            0.8,
        "distribution":            0.6,
        "crisis":                  0.0,           # halt
        "recovery":                0.7,
        "liquidation_cascade":     0.0,           # halt
        "squeeze":                 0.4,
        "uncertain":               0.4,
    }

    def __init__(
        self,
        kelly_fraction: float = 0.25,
        target_atr_pct: float = 0.008,
        min_size_usdt: float = 10.0,
        max_size_usdt: float = 0.0,
        max_capital_pct: float = 0.04,
        min_capital_pct: float = 0.003,
        loss_streak_trigger: int = 3,
        loss_streak_size_multiplier: float = 0.50,
        loss_streak_recovery_wins: int = 2,
        defensive_mode_multiplier: float = 0.25,
    ):
        """
        Initialize the position sizer using quarter-Kelly for safety.

        Parameters
        ----------
        kelly_fraction : float
            Fraction of Kelly Criterion to use (0.25 = quarter-Kelly for safety).
        target_atr_pct : float
            Target ATR as % of price (e.g., 0.008 = 0.8%).
        min_size_usdt : float
            Minimum position size in USDT.
        max_size_usdt : float
            Maximum position size in USDT. 0 = unlimited (constrained by capital_pct).
        max_capital_pct : float
            Maximum % of available capital per trade (e.g., 0.04 = 4% hard cap).
        min_capital_pct : float
            Minimum % of available capital per trade (e.g., 0.003 = 0.3% minimum).
        loss_streak_trigger : int
            Number of consecutive losses to trigger reduced sizing.
        loss_streak_size_multiplier : float
            Size multiplier when loss streak is active.
        loss_streak_recovery_wins : int
            Wins needed to recover from loss streak.
        defensive_mode_multiplier : float
            Size multiplier in crash/defensive mode.
        """
        self.kelly_fraction = kelly_fraction
        self.target_atr_pct = target_atr_pct
        self.min_size_usdt = min_size_usdt
        self.max_size_usdt = max_size_usdt
        self.max_capital_pct = max_capital_pct
        self.min_capital_pct = min_capital_pct
        self.loss_streak_trigger = loss_streak_trigger
        self.loss_streak_size_multiplier = loss_streak_size_multiplier
        self.loss_streak_recovery_wins = loss_streak_recovery_wins
        self.defensive_mode_multiplier = defensive_mode_multiplier
        # Internal state
        self._consecutive_losses: int = 0
        self._consecutive_wins: int = 0

    # ──────────────────────────────────────────────────────────────
    # Risk-Based Sizing (PRODUCTION DEFAULT)
    # ──────────────────────────────────────────────────────────────

    # ── Tiered capital limits (Phase 2 — gated behind capital.scaling_enabled) ──
    # Activated only when config capital.scaling_enabled = true.
    # Maps (open_position_count, high_conviction) → max_capital_pct override.
    _TIER_CAPS: dict[tuple, float] = {
        (0, False): 0.12,   # no open position, standard   → 12%
        (0, True):  0.18,   # no open position, high-conv  → 18%
        (1, False): 0.12,   # 1 open, standard             → 12%
        (1, True):  0.18,   # 1 open, high-conviction       → 18%
        (2, False): 0.08,   # 2 open                       →  8%
        (2, True):  0.08,
        (3, False): 0.05,   # 3+ open                      →  5%
        (3, True):  0.05,
    }
    _HIGH_CONVICTION_THRESHOLD: float = 0.70  # score ≥ 0.70 = high-conviction

    def calculate_risk_based(
        self,
        capital_usdt: float,
        entry_price: float,
        stop_price: float,
        risk_pct: float = 0.75,
        regime: str = "",
        drawdown_pct: float = 0.0,
        open_positions_count: int = 0,
        conviction_score: float = 0.0,
    ) -> float:
        """
        Calculate position size using risk-based formula.

        size = (risk_pct% × capital) / stop_distance × entry_price
        Capped at max_capital_pct (4% Phase 1; tiered when capital.scaling_enabled=true).
        Floored at min_size_usdt.

        Parameters
        ----------
        capital_usdt         : float — current total capital (USDT)
        entry_price          : float — expected fill price
        stop_price           : float — stop-loss price
        risk_pct             : float — % of capital to risk per trade (e.g. 0.75 = 0.75%)
        regime               : str   — market regime for halt check
        drawdown_pct         : float — current drawdown % for circuit-breaker check
        open_positions_count : int   — number of currently open positions (for tiered cap)
        conviction_score     : float — confluence score 0–1 (≥0.70 = high conviction tier)
        """
        if capital_usdt <= 0 or entry_price <= 0 or stop_price <= 0:
            return 0.0

        # Halt regime check
        if self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4) < 1e-9:
            logger.debug("PositionSizer: halt regime '%s' — size=0", regime)
            return 0.0

        stop_distance = abs(entry_price - stop_price)
        if stop_distance <= 0:
            logger.warning("PositionSizer: stop_distance=0 for entry=%.6f stop=%.6f — using min_size", entry_price, stop_price)
            return self.min_size_usdt

        # Risk-based core formula
        risk_usdt = (risk_pct / 100.0) * capital_usdt
        qty       = risk_usdt / stop_distance
        size_usdt = qty * entry_price

        # ── Tiered capital cap (Phase 2, gated behind capital.scaling_enabled) ──────
        # When enabled, overrides max_capital_pct with a concurrency-aware tier cap.
        # Default is Phase 1: self.max_capital_pct (4%).
        effective_max_pct = self.max_capital_pct
        try:
            from config.settings import settings as _s_cap
            if bool(_s_cap.get("capital.scaling_enabled", False)):
                _hi  = conviction_score >= self._HIGH_CONVICTION_THRESHOLD
                _cnt = min(open_positions_count, 3)   # cap at 3 for dict lookup
                effective_max_pct = self._TIER_CAPS.get((_cnt, _hi),
                                    self._TIER_CAPS[(3, _hi)])
                logger.debug(
                    "PositionSizer tiered cap: open=%d hi_conv=%s → max_cap=%.0f%%",
                    open_positions_count, _hi, effective_max_pct * 100,
                )
        except Exception:
            pass  # Fallback: use self.max_capital_pct

        # Cap at max_capital_pct of capital (e.g. 4% hard cap).
        # Using effective_max_pct ensures tiered model works correctly when enabled.
        cap_max = capital_usdt * effective_max_pct
        size_usdt = min(size_usdt, cap_max)

        # Absolute max_size_usdt cap — only applied when explicitly set (> 0).
        # For demo trading a hard dollar limit can be configured here.
        # With max_size_usdt=0 (default), the max_capital_pct cap above governs.
        if self.max_size_usdt > 0:
            size_usdt = min(size_usdt, self.max_size_usdt)

        # Floor
        size_usdt = max(size_usdt, self.min_size_usdt)

        logger.debug(
            "PositionSizer (risk-based): capital=%.2f risk_pct=%.2f%% "
            "stop_dist=%.6f qty=%.4f size=%.2f USDT "
            "(max_capital_pct=%.1f%% → cap=%.2f%s)",
            capital_usdt, risk_pct, stop_distance, qty, size_usdt,
            self.max_capital_pct * 100, cap_max,
            f" | abs_cap={self.max_size_usdt}" if self.max_size_usdt > 0 else "",
        )
        return round(size_usdt, 2)

    # ──────────────────────────────────────────────────────────────
    # Legacy Kelly Sizing (kept for backward compatibility)
    # ──────────────────────────────────────────────────────────────

    def calculate(
        self,
        available_capital_usdt: float,
        atr_value: float,
        entry_price: float,
        score: float,
        regime: str,
        drawdown_pct: float = 0.0,
        side: str = "long",
    ) -> float:
        """
        Calculate position size.

        When config risk_engine.sizing_mode == "risk_based" (default), this method
        is an alias for calculate_risk_based() using ATR as a stop-distance proxy.
        When sizing_mode == "kelly", uses the quarter-Kelly formula.

        For full risk-based sizing precision, call calculate_risk_based() directly
        with the exact stop_price from the OrderCandidate.

        Parameters
        ----------
        available_capital_usdt : float
            Total available trading capital (USDT).
        atr_value : float
            ATR14 value at entry price.
        entry_price : float
            Entry price (absolute, e.g., BTC/USDT price).
        score : float
            Confluence score (0.0–1.0). Affects position multiplier.
        regime : str
            Market regime (e.g., "bull_trend", "ranging", "crisis").
        drawdown_pct : float
            Current drawdown as percentage (0.0–100.0). Halts if >= 15%.
        side : str
            Trade side ("long" or "short"). Used for defensive mode check.

        Returns
        -------
        float
            Position size in USDT, rounded to 2 decimals.
            Returns 0.0 if regime is halt (crisis, liquidation_cascade).
        """
        if available_capital_usdt <= 0:
            return 0.0

        # ── Risk-based mode (PRODUCTION DEFAULT) ─────────────────────
        try:
            from config.settings import settings as _s
            sizing_mode = _s.get("risk_engine.sizing_mode", "risk_based")
        except Exception:
            sizing_mode = "risk_based"

        if sizing_mode == "risk_based":
            # Use ATR×1.5 as stop distance proxy (1.5 ATR stop = Study 4 config)
            stop_distance_proxy = atr_value * 1.5 if atr_value > 0 else entry_price * 0.01
            stop_price = (
                entry_price - stop_distance_proxy if side in ("buy", "long")
                else entry_price + stop_distance_proxy
            )
            risk_pct = float(_s.get("risk_engine.risk_pct_per_trade", 0.5))
            return self.calculate_risk_based(
                capital_usdt  = available_capital_usdt,
                entry_price   = entry_price,
                stop_price    = stop_price,
                risk_pct      = risk_pct,
                regime        = regime,
                drawdown_pct  = drawdown_pct,
            )

        # 1. Base Kelly allocation (legacy path)
        base_kelly = self.kelly_fraction * available_capital_usdt

        # 2. Volatility scalar (target_atr_pct / current_atr_pct)
        current_atr_pct = atr_value / entry_price if entry_price > 0 else 1.0
        if current_atr_pct > 0:
            vol_scalar = self.target_atr_pct / current_atr_pct
        else:
            vol_scalar = 1.0
        # Clamp to [0.2, 3.0]
        vol_scalar = max(0.2, min(3.0, vol_scalar))

        # 3. Regime multiplier
        regime_mult = self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4)
        if regime_mult < 1e-9:
            return 0.0  # Halt regime

        # 4. Score multiplier (confidence bonus)
        if score < 0.60:
            score_mult = 0.75
        elif score < 0.70:
            score_mult = 0.85
        elif score < 0.80:
            score_mult = 1.0
        elif score < 0.90:
            score_mult = 1.15
        else:
            score_mult = 1.3

        # 5. Drawdown scalar (linear interpolation with halts)
        drawdown_scalar = self._interpolate_drawdown_scalar(drawdown_pct)
        if drawdown_scalar < 1e-9:
            return 0.0  # Force halt at >= 15% drawdown

        # 6. Loss streak scalar
        loss_streak_scalar = self.loss_streak_scalar

        # 7. (Defensive scalar removed — crash-based auto-execution intervention
        #    disabled in production. Only hard control is the 10% drawdown
        #    circuit breaker in PaperExecutor.submit().)

        # 8. Combine all factors
        size = base_kelly * vol_scalar * regime_mult * score_mult * drawdown_scalar * loss_streak_scalar

        # 9. Capital percentage bounds
        cap_min = available_capital_usdt * self.min_capital_pct
        cap_max = available_capital_usdt * self.max_capital_pct
        size = max(cap_min, min(cap_max, size))

        # 10. Absolute max_size_usdt limit (if set)
        if self.max_size_usdt > 0:
            size = min(size, self.max_size_usdt)

        # 11. Minimum size floor
        size = max(self.min_size_usdt, size)

        return round(size, 2)

    def register_trade_outcome(self, won: bool) -> None:
        """
        Register the outcome of a trade (win/loss) and update streak counters.

        Parameters
        ----------
        won : bool
            True if trade was profitable, False otherwise
        """
        if won:
            self._consecutive_wins += 1
            if self._consecutive_wins >= self.loss_streak_recovery_wins:
                self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            self._consecutive_wins = 0

    @property
    def loss_streak_scalar(self) -> float:
        """
        Compute loss streak scalar multiplier.

        Returns
        -------
        float
            Size multiplier: loss_streak_size_multiplier if loss streak active, else 1.0
        """
        if self._consecutive_losses >= self.loss_streak_trigger:
            return self.loss_streak_size_multiplier
        return 1.0

    def _interpolate_drawdown_scalar(self, drawdown_pct: float) -> float:
        """
        Compute drawdown scalar with linear interpolation and halt thresholds.

        Breakpoints:
          dd=0%   → 1.0
          dd=5%   → 0.8
          dd=10%  → 0.6
          dd>=15% → 0.0 (halt)
        """
        if drawdown_pct < 0:
            return 1.0
        elif drawdown_pct >= 15.0:
            return 0.0  # Force halt
        elif drawdown_pct >= 10.0:
            # Linear interpolation between 10% (0.6) and 15% (0.0)
            return 0.6 * (15.0 - drawdown_pct) / 5.0
        elif drawdown_pct >= 5.0:
            # Linear interpolation between 5% (0.8) and 10% (0.6)
            return 0.8 - 0.2 * (drawdown_pct - 5.0) / 5.0
        else:
            # Linear interpolation between 0% (1.0) and 5% (0.8)
            return 1.0 - 0.2 * drawdown_pct / 5.0

    def is_halt_regime(self, regime: str) -> bool:
        """Return True if the regime has a risk multiplier of 0.0 (trading halt)."""
        return self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4) < 1e-9

    def get_regime_multiplier(self, regime: str) -> float:
        """Return the risk multiplier for a given regime."""
        return self.REGIME_RISK_MULTIPLIERS.get(regime, 0.4)

    # ──────────────────────────────────────────────────────────────
    # Pos-Frac Sizing (v1.3 — PBL + SLC models)
    # Active ONLY when mr_pbl_slc.enabled = true in config.yaml.
    # This is the EXACT sizing model from Phase 5 backtest (v7_final)
    # that produced CAGR=50.41% (zero fees) / CAGR=35.67% (maker fees).
    # ──────────────────────────────────────────────────────────────

    def calculate_pos_frac(
        self,
        capital_usdt: float,
        open_positions_count: int = 0,
        open_positions_by_symbol: Optional[dict] = None,
        symbol: str = "",
    ) -> float:
        """
        Calculate position size using pos-frac sizing from Phase 5 backtest.

        Formula:
          proposed_size = pos_frac × equity
          heat_after    = (deployed_est + proposed_size) / equity
          Rejected if heat_after > max_heat
          Rejected if open_positions_count >= max_positions
          Rejected if positions in `symbol` >= max_per_asset

        Parameters
        ----------
        capital_usdt : float
            Current total equity.
        open_positions_count : int
            Number of currently open positions across all symbols.
        open_positions_by_symbol : dict, optional
            symbol -> open position count (for max_per_asset check).
        symbol : str
            Symbol being evaluated.

        Returns
        -------
        float
            Position size in USDT, or 0.0 if a constraint is violated.
        """
        if capital_usdt <= 0:
            return 0.0

        try:
            from config.settings import settings as _s
            pos_frac      = float(_s.get("mr_pbl_slc.pos_frac",      0.35))
            max_heat      = float(_s.get("mr_pbl_slc.max_heat",      0.80))
            max_positions = int(  _s.get("mr_pbl_slc.max_positions", 10))
            max_per_asset = int(  _s.get("mr_pbl_slc.max_per_asset",  3))
        except Exception:
            pos_frac      = 0.35
            max_heat      = 0.80
            max_positions = 10
            max_per_asset = 3

        # ── Max positions gate ─────────────────────────────────────
        if open_positions_count >= max_positions:
            logger.debug(
                "PositionSizer.pos_frac: max_positions (%d/%d) — reject",
                open_positions_count, max_positions,
            )
            return 0.0

        # ── Max per asset gate ─────────────────────────────────────
        if symbol and open_positions_by_symbol:
            sym_count = open_positions_by_symbol.get(symbol, 0)
            if sym_count >= max_per_asset:
                logger.debug(
                    "PositionSizer.pos_frac: max_per_asset %s (%d/%d) — reject",
                    symbol, sym_count, max_per_asset,
                )
                return 0.0

        # ── Proposed size ──────────────────────────────────────────
        proposed_size = pos_frac * capital_usdt

        # ── Heat gate ─────────────────────────────────────────────
        # Conservative estimate: assume each open position uses pos_frac of equity.
        deployed_est = open_positions_count * pos_frac * capital_usdt
        heat_after   = (deployed_est + proposed_size) / capital_usdt
        if heat_after > max_heat:
            logger.debug(
                "PositionSizer.pos_frac: heat gate %.1f%% > %.1f%% — reject",
                heat_after * 100, max_heat * 100,
            )
            return 0.0

        logger.debug(
            "PositionSizer.pos_frac: equity=%.0f pos_frac=%.0f%% "
            "→ size=%.0f | heat=%.1f%% | open=%d",
            capital_usdt, pos_frac * 100, proposed_size,
            heat_after * 100, open_positions_count,
        )
        return round(proposed_size, 2)

    def is_pos_frac_mode_active(self) -> bool:
        """Return True when mr_pbl_slc pos-frac sizing should override risk-pct sizing."""
        try:
            from config.settings import settings as _s
            return bool(_s.get("mr_pbl_slc.enabled", False))
        except Exception:
            return False
