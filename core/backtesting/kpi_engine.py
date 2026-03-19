# ============================================================
# NEXUS TRADER — KPI Computation Engine
#
# Computes 20+ metrics from trade dictionaries for backtest
# evaluation, including Sharpe/Sortino/Calmar ratios,
# regime-based breakdowns, and per-model attribution.
# ============================================================

from dataclasses import dataclass, field
from typing import Optional
import numpy as np
from datetime import datetime


@dataclass
class SubKPIs:
    """KPI subset for a regime or model breakdown."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0

    profit_factor: float = 0.0
    avg_win_usdt: float = 0.0
    avg_loss_usdt: float = 0.0
    total_pnl_usdt: float = 0.0

    # R-multiple stats
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    expectancy_r: float = 0.0
    avg_rr_ratio: float = 0.0

    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0


@dataclass
class BacktestKPIs:
    """Complete KPI set for backtest evaluation."""

    # Core P&L
    net_profit_usdt: float = 0.0
    total_return_pct: float = 0.0

    # Win/Loss
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    loss_rate: float = 0.0

    # Profit Quality
    profit_factor: float = 0.0
    expectancy_r: float = 0.0  # E[R] = WR*avg_win_r - LR*avg_loss_r
    avg_win_usdt: float = 0.0
    avg_loss_usdt: float = 0.0
    avg_win_r: float = 0.0  # avg win in R-multiples
    avg_loss_r: float = 0.0  # avg loss in R-multiples
    avg_rr_ratio: float = 0.0  # reward/risk ratio

    # Risk
    max_drawdown_pct: float = 0.0
    max_drawdown_usdt: float = 0.0
    max_consecutive_losses: int = 0
    max_consecutive_wins: int = 0

    # Duration
    avg_trade_duration_bars: float = 0.0
    avg_trade_duration_hours: float = 0.0
    total_bars_in_market: int = 0
    exposure_pct: float = 0.0  # % of time in market

    # Risk-Adjusted
    sharpe_ratio: float = 0.0  # annualized
    sortino_ratio: float = 0.0  # downside-only volatility
    calmar_ratio: float = 0.0  # return / max DD

    # Long vs Short
    long_trades: int = 0
    short_trades: int = 0
    long_win_rate: float = 0.0
    short_win_rate: float = 0.0
    long_pnl_usdt: float = 0.0
    short_pnl_usdt: float = 0.0

    # Per-regime breakdown (dict of regime -> SubKPIs)
    by_regime: dict = field(default_factory=dict)

    # Per-model breakdown (dict of model_name -> SubKPIs)
    by_model: dict = field(default_factory=dict)


def compute_kpis(
    trades: list[dict],
    equity_curve: list[float],
    initial_capital: float,
    total_bars: int,
    timeframe: str = "1h",
) -> BacktestKPIs:
    """
    Compute comprehensive KPI set from backtest results.

    Args:
        trades: List of trade dicts with keys:
            - entry_price, exit_price, pnl, pnl_pct
            - side ("long"/"short") or direction ("long"/"short")
            - duration_bars
            - regime, models_fired, score (optional)
            - stop_loss, stop_loss_price, sl (optional)
        equity_curve: List of equity values over time
        initial_capital: Starting capital in USDT
        total_bars: Total bars in the backtest period
        timeframe: Bar timeframe ("1h", "15m", "5m", etc.)

    Returns:
        BacktestKPIs dataclass with all computed metrics
    """
    kpis = BacktestKPIs()

    if not trades or not equity_curve:
        return kpis

    # ── Core P&L ──────────────────────────────────────────
    final_equity = equity_curve[-1]
    kpis.net_profit_usdt = final_equity - initial_capital
    kpis.total_return_pct = (final_equity / initial_capital - 1.0) * 100.0

    # ── Win/Loss counts ────────────────────────────────────
    kpis.total_trades = len(trades)
    kpis.winning_trades = sum(1 for t in trades if t.get("pnl", 0) > 0)
    kpis.losing_trades = sum(1 for t in trades if t.get("pnl", 0) <= 0)

    if kpis.total_trades > 0:
        kpis.win_rate = kpis.winning_trades / kpis.total_trades
        kpis.loss_rate = kpis.losing_trades / kpis.total_trades

    # ── Profit Factor ──────────────────────────────────────
    gross_profit = sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) <= 0))
    if gross_loss > 0:
        kpis.profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        kpis.profit_factor = float("inf")
    else:
        kpis.profit_factor = 0.0

    # ── P&L per trade ─────────────────────────────────────
    winning_pnls = [t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0]
    losing_pnls = [t.get("pnl", 0) for t in trades if t.get("pnl", 0) <= 0]

    if winning_pnls:
        kpis.avg_win_usdt = np.mean(winning_pnls)
    if losing_pnls:
        kpis.avg_loss_usdt = np.mean(losing_pnls)

    # ── R-multiple stats ───────────────────────────────────
    r_multiples = _compute_r_multiples(trades)
    if r_multiples:
        winning_rs = [r for r in r_multiples if r > 0]
        losing_rs = [r for r in r_multiples if r <= 0]

        if winning_rs:
            kpis.avg_win_r = np.mean(winning_rs)
        if losing_rs:
            kpis.avg_loss_r = np.mean(losing_rs)

        # Expectancy = win_rate * avg_win_r - loss_rate * abs(avg_loss_r)
        if kpis.win_rate > 0 and kpis.avg_win_r > 0:
            kpis.expectancy_r = kpis.win_rate * kpis.avg_win_r - kpis.loss_rate * abs(
                kpis.avg_loss_r
            )

        # Average R:R ratio (reward/risk per trade)
        rr_ratios = []
        for r in r_multiples:
            if r > 0:
                # Winning trade: find its risk
                risk = abs(
                    min([x for x in r_multiples if x <= 0], default=1.0)
                )  # proxy
                if risk > 0:
                    rr_ratios.append(r / risk)
        if rr_ratios:
            kpis.avg_rr_ratio = np.mean(rr_ratios)

    # ── Drawdown ───────────────────────────────────────────
    peak_equity = initial_capital
    max_dd_usdt = 0.0
    for eq in equity_curve:
        if eq > peak_equity:
            peak_equity = eq
        dd = peak_equity - eq
        if dd > max_dd_usdt:
            max_dd_usdt = dd

    kpis.max_drawdown_usdt = max_dd_usdt
    if peak_equity > 0:
        kpis.max_drawdown_pct = (max_dd_usdt / peak_equity) * 100.0

    # ── Consecutive wins/losses ────────────────────────────
    kpis.max_consecutive_wins, kpis.max_consecutive_losses = _compute_streaks(trades)

    # ── Duration ───────────────────────────────────────────
    if kpis.total_trades > 0:
        total_duration_bars = sum(t.get("duration_bars", 0) for t in trades)
        kpis.avg_trade_duration_bars = total_duration_bars / kpis.total_trades
        kpis.total_bars_in_market = total_duration_bars

        # Convert bars to hours based on timeframe
        bars_per_hour = _bars_per_hour(timeframe)
        kpis.avg_trade_duration_hours = (
            kpis.avg_trade_duration_bars / bars_per_hour if bars_per_hour > 0 else 0.0
        )

        # Exposure: % of backtest period in a trade
        if total_bars > 0:
            kpis.exposure_pct = (total_duration_bars / total_bars) * 100.0

    # ── Risk-adjusted returns ──────────────────────────────
    if len(equity_curve) > 1:
        eq_arr = np.array(equity_curve, dtype=float)

        # Daily returns (proxy from equity curve)
        returns = np.diff(eq_arr) / eq_arr[:-1]

        # Sharpe Ratio (annualize based on timeframe)
        if len(returns) > 1 and np.std(returns) > 0:
            mean_ret = np.mean(returns)
            std_ret = np.std(returns)
            periods_per_year = _periods_per_year(timeframe)
            kpis.sharpe_ratio = (mean_ret / std_ret) * np.sqrt(periods_per_year)

        # Sortino Ratio (only negative returns in denominator)
        negative_returns = returns[returns < 0]
        if len(negative_returns) > 0 and np.std(negative_returns) > 0:
            mean_ret = np.mean(returns)
            downside_std = np.std(negative_returns)
            periods_per_year = _periods_per_year(timeframe)
            kpis.sortino_ratio = (mean_ret / downside_std) * np.sqrt(periods_per_year)

        # Calmar Ratio (return / max DD)
        if kpis.max_drawdown_pct > 0:
            kpis.calmar_ratio = kpis.total_return_pct / kpis.max_drawdown_pct

    # ── Long vs Short breakdown ────────────────────────────
    for t in trades:
        side = t.get("side") or t.get("direction", "").lower()
        if side == "long" or side == "buy":
            kpis.long_trades += 1
            kpis.long_pnl_usdt += t.get("pnl", 0)
        elif side == "short" or side == "sell":
            kpis.short_trades += 1
            kpis.short_pnl_usdt += t.get("pnl", 0)

    if kpis.long_trades > 0:
        long_wins = sum(
            1
            for t in trades
            if (t.get("side") or t.get("direction", "").lower()) in ("long", "buy")
            and t.get("pnl", 0) > 0
        )
        kpis.long_win_rate = long_wins / kpis.long_trades

    if kpis.short_trades > 0:
        short_wins = sum(
            1
            for t in trades
            if (t.get("side") or t.get("direction", "").lower()) in ("short", "sell")
            and t.get("pnl", 0) > 0
        )
        kpis.short_win_rate = short_wins / kpis.short_trades

    # ── Per-regime breakdown ───────────────────────────────
    trades_by_regime = {}
    for t in trades:
        regime = t.get("regime", "unknown").lower()
        if regime not in trades_by_regime:
            trades_by_regime[regime] = []
        trades_by_regime[regime].append(t)

    for regime, regime_trades in trades_by_regime.items():
        kpis.by_regime[regime] = _compute_sub_kpis(regime_trades)

    # ── Per-model breakdown ────────────────────────────────
    trades_by_model = {}
    for t in trades:
        models = t.get("models_fired", [])
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",")]
        for model in models:
            model_lower = model.lower().strip()
            if model_lower not in trades_by_model:
                trades_by_model[model_lower] = []
            trades_by_model[model_lower].append(t)

    for model, model_trades in trades_by_model.items():
        kpis.by_model[model] = _compute_sub_kpis(model_trades)

    return kpis


def _compute_sub_kpis(trades: list[dict]) -> SubKPIs:
    """Compute KPI subset for a group of trades (regime/model)."""
    sub = SubKPIs()

    if not trades:
        return sub

    sub.total_trades = len(trades)
    sub.winning_trades = sum(1 for t in trades if t.get("pnl", 0) > 0)
    sub.losing_trades = sum(1 for t in trades if t.get("pnl", 0) <= 0)

    if sub.total_trades > 0:
        sub.win_rate = sub.winning_trades / sub.total_trades
        sub.loss_rate = sub.losing_trades / sub.total_trades

    # Profit Factor
    gross_profit = sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0)
    gross_loss = abs(sum(t.get("pnl", 0) for t in trades if t.get("pnl", 0) <= 0))
    if gross_loss > 0:
        sub.profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        sub.profit_factor = float("inf")
    else:
        sub.profit_factor = 0.0

    # P&L per trade
    winning_pnls = [t.get("pnl", 0) for t in trades if t.get("pnl", 0) > 0]
    losing_pnls = [t.get("pnl", 0) for t in trades if t.get("pnl", 0) <= 0]
    sub.total_pnl_usdt = sum(t.get("pnl", 0) for t in trades)

    if winning_pnls:
        sub.avg_win_usdt = np.mean(winning_pnls)
    if losing_pnls:
        sub.avg_loss_usdt = np.mean(losing_pnls)

    # R-multiples
    r_multiples = _compute_r_multiples(trades)
    if r_multiples:
        winning_rs = [r for r in r_multiples if r > 0]
        losing_rs = [r for r in r_multiples if r <= 0]
        if winning_rs:
            sub.avg_win_r = np.mean(winning_rs)
        if losing_rs:
            sub.avg_loss_r = np.mean(losing_rs)
        if sub.win_rate > 0 and sub.avg_win_r > 0:
            sub.expectancy_r = sub.win_rate * sub.avg_win_r - sub.loss_rate * abs(
                sub.avg_loss_r
            )

    # Streaks
    sub.max_consecutive_wins, sub.max_consecutive_losses = _compute_streaks(trades)

    return sub


def _compute_r_multiples(trades: list[dict]) -> list[float]:
    """
    Compute R-multiples for each trade.

    R-multiple = (exit_price - entry_price) / initial_risk
    where initial_risk = |entry_price - stop_loss_price|
    """
    r_multiples = []

    for t in trades:
        entry = t.get("entry_price", 0.0)
        exit_px = t.get("exit_price", 0.0)
        sl = t.get("stop_loss_price") or t.get("sl")

        if entry and exit_px and sl:
            risk = abs(entry - sl)
            if risk > 0:
                reward = exit_px - entry
                r_multiple = reward / risk
                r_multiples.append(r_multiple)

    return r_multiples


def _compute_streaks(trades: list[dict]) -> tuple[int, int]:
    """Compute max consecutive wins and losses."""
    if not trades:
        return 0, 0

    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for t in trades:
        pnl = t.get("pnl", 0)
        if pnl > 0:
            current_wins += 1
            current_losses = 0
            if current_wins > max_wins:
                max_wins = current_wins
        else:
            current_losses += 1
            current_wins = 0
            if current_losses > max_losses:
                max_losses = current_losses

    return max_wins, max_losses


def _bars_per_hour(timeframe: str) -> float:
    """Convert timeframe to bars per hour."""
    timeframe_lower = timeframe.lower()
    if "1m" in timeframe_lower:
        return 60.0
    elif "5m" in timeframe_lower:
        return 12.0
    elif "15m" in timeframe_lower:
        return 4.0
    elif "30m" in timeframe_lower:
        return 2.0
    elif "1h" in timeframe_lower:
        return 1.0
    elif "4h" in timeframe_lower:
        return 0.25
    elif "1d" in timeframe_lower or "d" in timeframe_lower:
        return 1.0 / 24.0
    else:
        return 1.0  # default: 1h


def _periods_per_year(timeframe: str) -> float:
    """Convert timeframe to periods per year (for annualization)."""
    hours_per_year = 365.25 * 24
    bars_per_hour = _bars_per_hour(timeframe)
    return hours_per_year * bars_per_hour


def kpis_to_dict(kpis: BacktestKPIs) -> dict:
    """Convert BacktestKPIs dataclass to dict for serialization."""
    result = {
        # Core P&L
        "net_profit_usdt": round(kpis.net_profit_usdt, 2),
        "total_return_pct": round(kpis.total_return_pct, 2),
        # Win/Loss
        "total_trades": kpis.total_trades,
        "winning_trades": kpis.winning_trades,
        "losing_trades": kpis.losing_trades,
        "win_rate": round(kpis.win_rate, 4),
        "loss_rate": round(kpis.loss_rate, 4),
        # Profit Quality
        "profit_factor": round(kpis.profit_factor, 2) if kpis.profit_factor != float("inf") else "inf",
        "expectancy_r": round(kpis.expectancy_r, 4),
        "avg_win_usdt": round(kpis.avg_win_usdt, 2),
        "avg_loss_usdt": round(kpis.avg_loss_usdt, 2),
        "avg_win_r": round(kpis.avg_win_r, 4),
        "avg_loss_r": round(kpis.avg_loss_r, 4),
        "avg_rr_ratio": round(kpis.avg_rr_ratio, 4),
        # Risk
        "max_drawdown_pct": round(kpis.max_drawdown_pct, 2),
        "max_drawdown_usdt": round(kpis.max_drawdown_usdt, 2),
        "max_consecutive_losses": kpis.max_consecutive_losses,
        "max_consecutive_wins": kpis.max_consecutive_wins,
        # Duration
        "avg_trade_duration_bars": round(kpis.avg_trade_duration_bars, 2),
        "avg_trade_duration_hours": round(kpis.avg_trade_duration_hours, 2),
        "total_bars_in_market": kpis.total_bars_in_market,
        "exposure_pct": round(kpis.exposure_pct, 2),
        # Risk-Adjusted
        "sharpe_ratio": round(kpis.sharpe_ratio, 2),
        "sortino_ratio": round(kpis.sortino_ratio, 2),
        "calmar_ratio": round(kpis.calmar_ratio, 2),
        # Long vs Short
        "long_trades": kpis.long_trades,
        "short_trades": kpis.short_trades,
        "long_win_rate": round(kpis.long_win_rate, 4),
        "short_win_rate": round(kpis.short_win_rate, 4),
        "long_pnl_usdt": round(kpis.long_pnl_usdt, 2),
        "short_pnl_usdt": round(kpis.short_pnl_usdt, 2),
        # Breakdowns
        "by_regime": {
            regime: {
                "total_trades": sub.total_trades,
                "winning_trades": sub.winning_trades,
                "losing_trades": sub.losing_trades,
                "win_rate": round(sub.win_rate, 4),
                "profit_factor": round(sub.profit_factor, 2) if sub.profit_factor != float("inf") else "inf",
                "total_pnl_usdt": round(sub.total_pnl_usdt, 2),
                "avg_win_r": round(sub.avg_win_r, 4),
                "avg_loss_r": round(sub.avg_loss_r, 4),
                "expectancy_r": round(sub.expectancy_r, 4),
                "max_consecutive_losses": sub.max_consecutive_losses,
            }
            for regime, sub in kpis.by_regime.items()
        },
        "by_model": {
            model: {
                "total_trades": sub.total_trades,
                "winning_trades": sub.winning_trades,
                "losing_trades": sub.losing_trades,
                "win_rate": round(sub.win_rate, 4),
                "profit_factor": round(sub.profit_factor, 2) if sub.profit_factor != float("inf") else "inf",
                "total_pnl_usdt": round(sub.total_pnl_usdt, 2),
                "avg_win_r": round(sub.avg_win_r, 4),
                "avg_loss_r": round(sub.avg_loss_r, 4),
                "expectancy_r": round(sub.expectancy_r, 4),
                "max_consecutive_losses": sub.max_consecutive_losses,
            }
            for model, sub in kpis.by_model.items()
        },
    }

    return result


def dict_to_kpis(data: dict) -> BacktestKPIs:
    """Reconstruct BacktestKPIs from dict (inverse of kpis_to_dict)."""
    kpis = BacktestKPIs()

    # Core P&L
    kpis.net_profit_usdt = data.get("net_profit_usdt", 0.0)
    kpis.total_return_pct = data.get("total_return_pct", 0.0)

    # Win/Loss
    kpis.total_trades = data.get("total_trades", 0)
    kpis.winning_trades = data.get("winning_trades", 0)
    kpis.losing_trades = data.get("losing_trades", 0)
    kpis.win_rate = data.get("win_rate", 0.0)
    kpis.loss_rate = data.get("loss_rate", 0.0)

    # Profit Quality
    pf = data.get("profit_factor", 0.0)
    kpis.profit_factor = float("inf") if pf == "inf" else pf
    kpis.expectancy_r = data.get("expectancy_r", 0.0)
    kpis.avg_win_usdt = data.get("avg_win_usdt", 0.0)
    kpis.avg_loss_usdt = data.get("avg_loss_usdt", 0.0)
    kpis.avg_win_r = data.get("avg_win_r", 0.0)
    kpis.avg_loss_r = data.get("avg_loss_r", 0.0)
    kpis.avg_rr_ratio = data.get("avg_rr_ratio", 0.0)

    # Risk
    kpis.max_drawdown_pct = data.get("max_drawdown_pct", 0.0)
    kpis.max_drawdown_usdt = data.get("max_drawdown_usdt", 0.0)
    kpis.max_consecutive_losses = data.get("max_consecutive_losses", 0)
    kpis.max_consecutive_wins = data.get("max_consecutive_wins", 0)

    # Duration
    kpis.avg_trade_duration_bars = data.get("avg_trade_duration_bars", 0.0)
    kpis.avg_trade_duration_hours = data.get("avg_trade_duration_hours", 0.0)
    kpis.total_bars_in_market = data.get("total_bars_in_market", 0)
    kpis.exposure_pct = data.get("exposure_pct", 0.0)

    # Risk-Adjusted
    kpis.sharpe_ratio = data.get("sharpe_ratio", 0.0)
    kpis.sortino_ratio = data.get("sortino_ratio", 0.0)
    kpis.calmar_ratio = data.get("calmar_ratio", 0.0)

    # Long vs Short
    kpis.long_trades = data.get("long_trades", 0)
    kpis.short_trades = data.get("short_trades", 0)
    kpis.long_win_rate = data.get("long_win_rate", 0.0)
    kpis.short_win_rate = data.get("short_win_rate", 0.0)
    kpis.long_pnl_usdt = data.get("long_pnl_usdt", 0.0)
    kpis.short_pnl_usdt = data.get("short_pnl_usdt", 0.0)

    # Breakdowns
    if "by_regime" in data:
        for regime, sub_data in data["by_regime"].items():
            sub = SubKPIs()
            sub.total_trades = sub_data.get("total_trades", 0)
            sub.winning_trades = sub_data.get("winning_trades", 0)
            sub.losing_trades = sub_data.get("losing_trades", 0)
            sub.win_rate = sub_data.get("win_rate", 0.0)
            sub.profit_factor = sub_data.get("profit_factor", 0.0)
            sub.total_pnl_usdt = sub_data.get("total_pnl_usdt", 0.0)
            sub.avg_win_r = sub_data.get("avg_win_r", 0.0)
            sub.avg_loss_r = sub_data.get("avg_loss_r", 0.0)
            sub.expectancy_r = sub_data.get("expectancy_r", 0.0)
            sub.max_consecutive_losses = sub_data.get("max_consecutive_losses", 0)
            kpis.by_regime[regime] = sub

    if "by_model" in data:
        for model, sub_data in data["by_model"].items():
            sub = SubKPIs()
            sub.total_trades = sub_data.get("total_trades", 0)
            sub.winning_trades = sub_data.get("winning_trades", 0)
            sub.losing_trades = sub_data.get("losing_trades", 0)
            sub.win_rate = sub_data.get("win_rate", 0.0)
            sub.profit_factor = sub_data.get("profit_factor", 0.0)
            sub.total_pnl_usdt = sub_data.get("total_pnl_usdt", 0.0)
            sub.avg_win_r = sub_data.get("avg_win_r", 0.0)
            sub.avg_loss_r = sub_data.get("avg_loss_r", 0.0)
            sub.expectancy_r = sub_data.get("expectancy_r", 0.0)
            sub.max_consecutive_losses = sub_data.get("max_consecutive_losses", 0)
            kpis.by_model[model] = sub

    return kpis
