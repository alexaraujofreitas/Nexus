# ============================================================
# NEXUS TRADER — Backtesting Page
# Strategy runner + equity curve + metrics + trade log
# ============================================================

import json
import logging
from typing import Optional

import numpy as np
import pyqtgraph as pg

from datetime import datetime, timedelta

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QFrame,
    QPushButton, QComboBox, QDoubleSpinBox, QProgressBar,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QSplitter, QSizePolicy, QScrollArea, QAbstractItemView,
    QLineEdit, QDateEdit, QDialog, QTextEdit, QMessageBox
)
from PySide6.QtCore import Qt, QThread, Signal, Slot, QDate
from PySide6.QtGui import QColor, QFont, QBrush

from gui.main_window import PageHeader

logger = logging.getLogger(__name__)

# ── Style constants ───────────────────────────────────────────
_C_BG    = "#0A0E1A"
_C_CARD  = "#0F1623"
_C_TEXT  = "#E8EBF0"
_C_MUTED = "#8899AA"
_C_BULL  = "#00CC77"
_C_BEAR  = "#FF3355"
_C_GOLD  = "#FFB300"

_COMBO = (
    "QComboBox { background:#131B2A; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:3px 8px; font-size:12px; min-height:28px; }"
    "QComboBox:focus { border-color:#1E90FF; }"
    "QComboBox QAbstractItemView { background:#131B2A; color:#E8EBF0; "
    "selection-background-color:#1A2D4A; }"
)
_SPIN = (
    "QDoubleSpinBox { background:#131B2A; color:#E8EBF0; border:1px solid #2A3A52; "
    "border-radius:4px; padding:3px 8px; font-size:12px; min-height:28px; }"
)
_BTN = (
    "QPushButton { background:#1A2332; color:#8899AA; border:1px solid #2A3A52; "
    "border-radius:4px; font-size:11px; padding:2px 10px; }"
    "QPushButton:hover { color:#E8EBF0; border-color:#4A6A8A; }"
)


# ── Custom pyqtgraph axis items ───────────────────────────────
class _DateAxisItem(pg.AxisItem):
    """
    X-axis that displays Unix-second timestamps as human-readable dates.

    - tickSpacing() returns calendar-aligned intervals so labels never cramp.
    - tickStrings() adapts the format (year / month / day / hour) to the
      visible span.
    - Values near 0 (pyqtgraph's default empty-chart range) are suppressed
      so the Jan-01-1970 artefact never appears.
    """

    # (seconds_threshold, major_spacing, label_format)
    # All formats are single-line so labels are never clipped at the axis edge.
    _LEVELS = [
        (5 * 365 * 86400,  2 * 365 * 86400, "%Y"),
        (2 * 365 * 86400,      365 * 86400, "%Y"),
        (    365 * 86400,       90 * 86400, "%b '%y"),
        (     90 * 86400,       30 * 86400, "%b '%y"),
        (     30 * 86400,        7 * 86400, "%b %d"),
        (     14 * 86400,        2 * 86400, "%b %d"),
        (      3 * 86400,            86400, "%b %d"),
        (          86400,         6 * 3600, "%b %d %H:%M"),
        (       6 * 3600,             3600, "%H:%M"),
        (           3600,          15 * 60, "%H:%M"),
        (              0,          15 * 60, "%H:%M"),
    ]
    # Unix timestamps before 2000-01-01 are treated as "no data" (default range)
    _MIN_VALID_TS = 946684800  # 2000-01-01 00:00:00 UTC

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Provide extra vertical room so single-line date labels are never clipped.
        self.setHeight(42)

    def tickSpacing(self, minVal, maxVal, size):
        span = maxVal - minVal
        if span <= 0 or maxVal < self._MIN_VALID_TS:
            # No real data — return a large spacing so no labels are drawn
            return [(1e12, 0)]
        for threshold, major, _ in self._LEVELS:
            if span >= threshold:
                return [(major, 0), (major / 4, 0)]
        return [(15 * 60, 0)]

    def tickStrings(self, values, scale, spacing):
        from datetime import datetime as _dt
        if not values:
            return []
        valid = [v for v in values if v >= self._MIN_VALID_TS]
        if not valid:
            return [""] * len(values)
        span = max(valid) - min(valid)
        fmt = "%b %d\n%Y"
        for threshold, _, label_fmt in self._LEVELS:
            if span >= threshold:
                fmt = label_fmt
                break
        result = []
        for v in values:
            if v < self._MIN_VALID_TS:
                result.append("")
                continue
            try:
                result.append(_dt.utcfromtimestamp(float(v)).strftime(fmt))
            except Exception:
                result.append("")
        return result


class _CommaAxisItem(pg.AxisItem):
    """Y-axis that formats numbers with thousands separators (e.g., 10,000)."""

    def tickStrings(self, values, scale, spacing):
        result = []
        for v in values:
            try:
                if abs(v) >= 1000:
                    result.append(f"{v:,.0f}")
                else:
                    result.append(f"{v:.2f}")
            except Exception:
                result.append(str(v))
        return result


# ── Spinbox with thousands-separator display ──────────────────
class _FormattedSpinBox(QDoubleSpinBox):
    """QDoubleSpinBox that shows values with commas (e.g. 10,000)."""

    def textFromValue(self, value: float) -> str:
        dec = self.decimals()
        return f"{value:,.{dec}f}"

    def valueFromText(self, text: str) -> float:
        # Strip commas, prefix and suffix before float conversion
        s = text
        for tok in (self.suffix(), self.prefix()):
            if tok:
                s = s.replace(tok, "")
        s = s.replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return self.value()

    def validate(self, text: str, pos: int):
        from PySide6.QtGui import QValidator
        # Pass cleaned text (no commas) to the parent validator so Qt
        # doesn't reject comma-containing strings as invalid.
        clean = text
        for tok in (self.suffix(), self.prefix()):
            if tok:
                clean = clean.replace(tok, "")
        clean = clean.replace(",", "")
        state, _, _ = super().validate(clean.strip(), pos)
        return state, text, pos


# ── Metric card widget ────────────────────────────────────────
class MetricCard(QFrame):
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setMinimumWidth(140)
        self.setMinimumHeight(64)
        v = QVBoxLayout(self); v.setContentsMargins(12, 8, 12, 8); v.setSpacing(3)
        self._title = QLabel(title)
        self._title.setStyleSheet("color:#8899AA; font-size:9px; font-weight:600; "
                                  "")
        self._title.setWordWrap(False)
        self._value = QLabel("—")
        self._value.setStyleSheet("color:#E8EBF0; font-size:17px; font-weight:700;")
        self._value.setWordWrap(False)
        v.addWidget(self._title)
        v.addWidget(self._value)

    def set_value(self, text: str, color: str = "#E8EBF0"):
        self._value.setText(text)
        self._value.setStyleSheet(
            f"color:{color}; font-size:17px; font-weight:700;")


# ── Background backtest worker ────────────────────────────────
class BacktestWorker(QThread):
    progress  = Signal(str)
    finished  = Signal(dict)
    error     = Signal(str)

    def __init__(self, strategy: dict, df_or_map, primary_tf: str,
                 config: dict, parent=None):
        super().__init__(parent)
        self._strategy   = strategy
        self._primary_tf = primary_tf
        self._config     = config
        # Accept either a single DataFrame (single-TF) or {tf: df} dict (multi-TF)
        if isinstance(df_or_map, dict):
            self._df_map = df_or_map
            self._df     = None
        else:
            self._df_map = None
            self._df     = df_or_map

    def run(self):
        try:
            from core.backtesting.backtest_engine import run_backtest

            stype = self._strategy.get("type", "rule")
            defn  = self._strategy.get("definition") or {}

            # ── Route by strategy type ────────────────────────
            # Also detect by definition structure: AI strategies have "entry_long"
            # key; rule strategies have "entry.rule_tree". This handles strategies
            # that were saved before the type field was set correctly.
            is_ai = (
                stype in ("ai", "ml", "ensemble")
                or self._strategy.get("ai_generated", False)
                or "entry_long" in defn
            )
            if is_ai:
                self._run_ai_strategy(run_backtest, defn)
            else:
                self._run_rule_strategy(run_backtest, defn)

        except Exception as e:
            logger.error("BacktestWorker: %s", e, exc_info=True)
            self.error.emit(str(e))

    def _run_rule_strategy(self, run_backtest, defn: dict):
        """Handle classic Rule-Builder strategy definition."""
        self.progress.emit("Calculating indicators…")
        entry = defn.get("entry", {})
        exit_ = defn.get("exit",  {})

        entry_tree = entry.get("rule_tree")
        exit_tree  = exit_.get("rule_tree")

        if not entry_tree:
            self.error.emit("Strategy has no entry rule tree. Build one in the Rule Builder.")
            return

        self.progress.emit("Running simulation…")
        common_kwargs = dict(
            initial_capital   = self._config.get("initial_capital", 10000.0),
            position_size_pct = defn.get("position_size_pct", 10.0),
            stop_loss_pct     = exit_.get("stop_loss_pct", 2.0),
            take_profit_pct   = exit_.get("take_profit_pct", 4.0),
            fee_pct           = defn.get("fee_pct", 0.1),
            slippage_pct      = defn.get("slippage_pct", 0.05),
            direction         = entry.get("direction", "long"),
        )
        if self._df_map and self._primary_tf:
            # Multi-TF path
            result = run_backtest(
                entry_tree, exit_tree,
                df_map=self._df_map,
                primary_tf=self._primary_tf,
                **common_kwargs,
            )
        else:
            result = run_backtest(
                entry_tree, exit_tree,
                df_raw=self._df,
                **common_kwargs,
            )
        self._emit_result(result)

    def _run_ai_strategy(self, run_backtest, defn: dict):
        """Handle AI-generated strategy definition — parse text conditions."""
        self.progress.emit("Parsing AI strategy conditions…")
        from core.ai.condition_parser import ai_definition_to_backtest_params

        params = ai_definition_to_backtest_params(defn)

        if params["parse_errors"]:
            for err in params["parse_errors"]:
                logger.warning("AI condition parser: %s", err)

        if not params["entry_tree"]:
            self.error.emit(
                "Could not parse entry conditions from this AI strategy.\n\n"
                "Try asking the AI to regenerate the strategy with more explicit "
                "conditions, e.g. 'RSI crosses above 30' or 'price above EMA20'."
            )
            return

        self.progress.emit("Running simulation…")
        ai_kwargs = dict(
            initial_capital   = self._config.get("initial_capital", 10000.0),
            position_size_pct = params["position_size_pct"],
            stop_loss_pct     = params["stop_loss_pct"],
            take_profit_pct   = params["take_profit_pct"],
            fee_pct           = self._config.get("fee_pct", 0.1),
            slippage_pct      = self._config.get("slippage_pct", 0.05),
            direction         = params["direction"],
        )
        if self._df_map and self._primary_tf:
            result = run_backtest(
                params["entry_tree"], params["exit_tree"],
                df_map=self._df_map,
                primary_tf=self._primary_tf,
                **ai_kwargs,
            )
        else:
            # Fallback: extract single df from df_map or use self._df
            df_raw = (list(self._df_map.values())[0]
                      if self._df_map else self._df)
            result = run_backtest(
                params["entry_tree"], params["exit_tree"],
                df_raw=df_raw,
                **ai_kwargs,
            )
        if params["parse_errors"]:
            result["parse_warnings"] = params["parse_errors"]
        self._emit_result(result)

    def _emit_result(self, result: dict):
        result["strategy_name"]     = self._strategy.get("name", "Unnamed")
        result["strategy_id"]       = self._strategy.get("id")
        result["strategy_type"]     = self._strategy.get("type", "rule")
        result["symbol"]            = self._config.get("symbol", "")
        result["timeframe"]         = self._config.get("timeframe", "1h")
        result["initial_capital"]   = self._config.get("initial_capital", 10000.0)
        # Record which TFs were actually used (primary + any secondary)
        result["loaded_timeframes"] = self._config.get("loaded_timeframes", [])
        self.progress.emit("Done")
        self.finished.emit(result)


# ── AI Strategy Optimizer worker ─────────────────────────────
class OptimizeWorker(QThread):
    """
    Runs the AI strategy optimizer in a background thread.
    Emits finished(new_strategy_dict, changes_text) or error(message).
    """
    progress = Signal(str)
    finished = Signal(dict, str)   # (new_strategy, changes_markdown)
    error    = Signal(str)

    def __init__(self, strategy: dict, result: dict, parent=None):
        super().__init__(parent)
        self._strategy = strategy
        self._result   = result

    def run(self):
        try:
            from core.ai.llm_provider import get_provider, LLMMessage, OllamaProvider
            from core.ai.strategy_optimizer import (
                OPTIMIZER_SYSTEM_PROMPT,
                build_optimizer_prompt,
                extract_optimizer_response,
                next_version_name,
            )

            provider = get_provider()
            if not provider:
                self.error.emit(
                    "No AI API key is configured.\n"
                    "Add your Anthropic or OpenAI key in Settings → AI."
                )
                return

            metrics = self._result.get("metrics", {})
            trades  = self._result.get("trades",  [])
            symbol  = self._result.get("symbol",  "?")
            tf      = self._result.get("timeframe", "?")

            # Build human-readable date range from equity timestamps
            ts_list    = self._result.get("equity_timestamps") or []
            date_range = ""
            if ts_list:
                try:
                    import pandas as pd
                    date_range = (
                        f"{pd.Timestamp(ts_list[0]).strftime('%Y-%m-%d')} → "
                        f"{pd.Timestamp(ts_list[-1]).strftime('%Y-%m-%d')}"
                    )
                except Exception:
                    pass

            # Determine the versioned name before calling the LLM so we can
            # tell the model exactly what to name the strategy.
            base_name = self._strategy.get("name", "Strategy")
            new_name  = next_version_name(base_name)

            self.progress.emit("🤖 Analyzing backtest results…")

            # Local Ollama models have limited effective context — keep the
            # losing-trade sample small so the full prompt fits comfortably.
            is_local = isinstance(provider, OllamaProvider)
            trade_sample = 10 if is_local else 30

            user_prompt = build_optimizer_prompt(
                self._strategy, metrics, trades,
                symbol, tf, new_name, date_range,
                max_trade_sample=trade_sample,
            )

            messages = [LLMMessage(role="user", content=user_prompt)]

            wait_msg = (
                "🤖 Generating optimized strategy (local model — may take 1-3 min)…"
                if is_local else
                "🤖 Generating optimized strategy (this may take 20-30 s)…"
            )
            self.progress.emit(wait_msg)

            # Stream and collect full response
            full_text = ""
            for chunk in provider.stream_chat(
                messages,
                system_prompt=OPTIMIZER_SYSTEM_PROMPT,
                max_tokens=8000,
            ):
                full_text += chunk

            self.progress.emit("🤖 Parsing optimizer response…")

            # Log the raw response for debugging (first 500 chars)
            logger.debug(
                "OptimizeWorker raw response (%d chars): %s…",
                len(full_text),
                full_text[:500].replace("\n", " "),
            )

            proposal, changes = extract_optimizer_response(full_text)

            if not proposal:
                # Build a helpful hint based on provider type
                if is_local:
                    hint = (
                        "The local Ollama model did not produce a complete strategy.\n\n"
                        "Tips:\n"
                        "• Make sure Ollama is still running (open PowerShell → ollama serve)\n"
                        "• Try again — local models occasionally fail on complex tasks\n"
                        "• For more reliable results, switch to Anthropic Claude or Google Gemini "
                        "in Settings → AI Provider"
                    )
                else:
                    hint = (
                        "The AI did not return a valid strategy definition.\n"
                        "The response may have been cut off — please try again."
                    )
                logger.warning(
                    "OptimizeWorker: no <strategy_config> found. "
                    "Full response (%d chars): %s",
                    len(full_text),
                    full_text[:2000],
                )
                self.error.emit(hint)
                return

            changes = changes or "No detailed change log was returned."

            # Enforce correct name and preserve original symbol
            proposal["name"] = new_name
            defn = proposal.get("definition") or {}
            orig_symbols = (self._strategy.get("definition") or {}).get("symbols", [symbol])
            defn.setdefault("symbols", orig_symbols)
            proposal["definition"] = defn

            # ── Validate that entry conditions can be parsed ──
            self.progress.emit("🔍 Validating condition format…")
            from core.ai.condition_parser import parse_condition_text

            entry_long  = defn.get("entry_long",  {}) or {}
            entry_short = defn.get("entry_short", {}) or {}
            entry_conds = [c for c in (entry_long.get("conditions") or entry_short.get("conditions") or []) if c.strip()]

            # Check 1: no conditions at all — the model generated an incomplete definition
            if not entry_conds:
                local_note = (
                    "\n\nThe local model likely ran out of context while generating the JSON.\n"
                    "Try again, or switch to Anthropic Claude / Google Gemini in Settings → AI Provider."
                ) if is_local else (
                    "\n\nThe AI may have returned a truncated response. Please try again."
                )
                logger.warning(
                    "OptimizeWorker: strategy definition has no entry conditions. "
                    "Full definition: %s", json.dumps(defn)
                )
                self.error.emit(
                    "⚠ The AI returned a strategy definition with no entry conditions.\n"
                    "The optimized strategy cannot be used for backtesting." + local_note
                )
                return

            bad_conds   = [c for c in entry_conds if parse_condition_text(c) is None]
            good_conds  = [c for c in entry_conds if parse_condition_text(c) is not None]

            # Check 2: every condition is in a bad format
            if bad_conds and not good_conds:
                bad_list = "\n".join(f"  • {c}" for c in bad_conds)
                self.error.emit(
                    "⚠ The AI returned entry conditions in an unsupported format:\n\n"
                    f"{bad_list}\n\n"
                    "Please try optimizing again — the AI occasionally ignores the format rules. "
                    "Switching to a different AI provider in Settings may also help."
                )
                return

            if bad_conds:
                # Some conditions are bad but at least one is good — log and strip bad ones
                logger.warning(
                    "OptimizeWorker: removing %d unparseable condition(s): %s",
                    len(bad_conds), bad_conds,
                )
                entry_long["conditions"] = good_conds
                defn["entry_long"] = entry_long
                changes = (changes or "") + (
                    f"\n\n⚠ Note: {len(bad_conds)} condition(s) were removed because they used an "
                    "unsupported format that the backtest engine cannot parse."
                )

            # ── Persist to database ───────────────────────────
            self.progress.emit("💾 Saving optimized strategy…")
            from core.database.engine import get_session
            from core.database.models import Strategy as StrategyModel

            with get_session() as s:
                strat = StrategyModel(
                    name            = new_name,
                    description     = proposal.get("description", ""),
                    type            = "ai",
                    status          = "draft",
                    lifecycle_stage = 1,
                    definition      = proposal.get("definition", {}),
                    ai_generated    = True,
                    ai_model_used   = provider.model_name,
                    created_by      = "optimizer",
                )
                s.add(strat)
                s.flush()
                strat_id = strat.id

            new_strategy = {
                "id":              strat_id,
                "name":            new_name,
                "type":            "ai",
                "lifecycle_stage": 1,
                "ai_generated":    True,
                "definition":      proposal.get("definition", {}),
            }

            self.finished.emit(new_strategy, changes)

        except Exception as exc:
            logger.error("OptimizeWorker error: %s", exc, exc_info=True)
            self.error.emit(str(exc))


# ── Optimization result dialog ────────────────────────────────
class OptimizeResultDialog(QDialog):
    """
    Shows the AI's change-log after optimization and lets the user
    immediately queue a backtest of the new strategy.
    """
    backtest_requested = Signal(dict)   # emits the new strategy dict

    def __init__(self, new_strategy: dict, changes: str, parent=None):
        super().__init__(parent)
        self._strategy = new_strategy
        self.setWindowTitle("Strategy Optimized")
        self.setMinimumWidth(580)
        self.setMinimumHeight(440)
        self.setStyleSheet(f"background:{_C_BG}; color:{_C_TEXT};")

        v = QVBoxLayout(self)
        v.setContentsMargins(24, 20, 24, 20)
        v.setSpacing(12)

        # ── Header ────────────────────────────────────────────
        hdr = QLabel("⚡  Strategy Optimized & Saved")
        hdr.setStyleSheet(
            "font-size:16px; font-weight:700; color:#FFB300; padding-bottom:2px;"
        )
        v.addWidget(hdr)

        name_lbl = QLabel(f"New strategy:  <b>{new_strategy['name']}</b>")
        name_lbl.setStyleSheet("font-size:12px; color:#8899AA;")
        name_lbl.setTextFormat(Qt.RichText)
        v.addWidget(name_lbl)

        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("color:#1A2A3A; margin-top:4px; margin-bottom:4px;")
        v.addWidget(sep)

        # ── Changes log ───────────────────────────────────────
        chg_hdr = QLabel("WHAT THE AI CHANGED")
        chg_hdr.setStyleSheet(
            "color:#8899AA; font-size:9px; font-weight:600; "
        )
        v.addWidget(chg_hdr)

        changes_box = QTextEdit()
        changes_box.setReadOnly(True)
        changes_box.setPlainText(changes)
        changes_box.setStyleSheet(
            "QTextEdit { background:#080E1A; color:#C8D8E8; "
            "border:1px solid #1A2A3A; border-radius:5px; "
            "font-size:12px; line-height:1.5; padding:10px; }"
        )
        v.addWidget(changes_box, 1)

        # ── Note about overfitting ─────────────────────────────
        note = QLabel(
            "⚠  Tip: backtest the optimized strategy on a <i>different</i> date range "
            "than the one used for optimization to check for overfitting."
        )
        note.setStyleSheet("color:#5A7A9A; font-size:10px; font-style:italic;")
        note.setWordWrap(True)
        v.addWidget(note)

        # ── Buttons ───────────────────────────────────────────
        btn_row = QHBoxLayout(); btn_row.addStretch()

        close_btn = QPushButton("Close")
        close_btn.setFixedHeight(32)
        close_btn.setStyleSheet(
            "QPushButton { background:#1A2332; color:#8899AA; border:1px solid #2A3A52; "
            "border-radius:4px; font-size:12px; padding:0 18px; }"
            "QPushButton:hover { color:#E8EBF0; border-color:#4A6A8A; }"
        )
        close_btn.clicked.connect(self.accept)

        bt_btn = QPushButton("▶  Backtest Optimized Strategy")
        bt_btn.setFixedHeight(32)
        bt_btn.setStyleSheet(
            "QPushButton { background:#E87820; color:#FFF; border:none; "
            "border-radius:4px; font-size:12px; font-weight:700; padding:0 18px; }"
            "QPushButton:hover { background:#FF8C30; }"
        )
        bt_btn.clicked.connect(self._on_backtest_clicked)

        btn_row.addWidget(close_btn)
        btn_row.addSpacing(8)
        btn_row.addWidget(bt_btn)
        v.addLayout(btn_row)

    def _on_backtest_clicked(self):
        self.backtest_requested.emit(self._strategy)
        self.accept()


# ── Main backtesting page ─────────────────────────────────────
# ── IDSS pipeline backtest worker ─────────────────────────────
class IDSSBacktestWorker(QThread):
    """
    Background thread that:
      1. Runs calculate_all() on a pre-loaded OHLCV DataFrame
      2. Feeds it bar-by-bar through the full IDSS pipeline
      3. Emits a result dict compatible with _render_results()
    """
    progress = Signal(str)
    finished = Signal(dict)
    error    = Signal(str)

    def __init__(
        self,
        symbol:    str,
        timeframe: str,
        df,                 # raw OHLCV DataFrame (indicators NOT yet computed)
        config:    dict,
        parent=None,
    ):
        super().__init__(parent)
        self._symbol    = symbol
        self._timeframe = timeframe
        self._df        = df
        self._config    = config

    def run(self):
        try:
            from core.features.indicator_library import calculate_all
            from core.backtesting.idss_backtester import IDSSBacktester
            from config.settings import settings as _s

            self.progress.emit(f"Computing indicators for {self._symbol} [{self._timeframe}]…")
            df_calc = calculate_all(self._df.copy())

            self.progress.emit(f"Starting IDSS pipeline replay ({len(df_calc):,} bars)…")

            bt = IDSSBacktester(
                min_confluence_score = float(_s.get("idss.min_confluence_score", 0.55)),
                min_risk_reward      = float(_s.get("risk.min_risk_reward", 1.3)),
                position_size_pct    = 10.0,   # 10 % of equity per trade
            )

            result = bt.run(
                df              = df_calc,
                symbol          = self._symbol,
                timeframe       = self._timeframe,
                initial_capital = self._config.get("initial_capital", 10_000.0),
                fee_pct         = self._config.get("fee_pct", 0.10),
                slippage_pct    = self._config.get("slippage_pct", 0.05),
                progress_cb     = lambda msg: self.progress.emit(msg),
            )
            self.finished.emit(result)

        except Exception as exc:
            logger.error("IDSSBacktestWorker: %s", exc, exc_info=True)
            self.error.emit(str(exc))


class BacktestingPage(QWidget):
    """
    Backtesting page with:
    - Strategy + symbol + timeframe selector
    - Capital / fee override spinboxes
    - Run Backtest button with progress feedback
    - IDSS Pipeline quick-run section (symbol + TF only, no stored strategy needed)
    - Equity curve pyqtgraph chart
    - Performance metric cards
    - Trade log table
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker:         Optional[BacktestWorker]     = None
        self._idss_worker:    Optional[IDSSBacktestWorker] = None
        self._fetch_worker    = None                        # BinanceMultiTFWorker
        self._optimize_worker: Optional[OptimizeWorker]    = None
        self._current_result: Optional[dict]               = None
        self._build()

    def _build(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(0)
        layout.addWidget(PageHeader(
            "Backtesting",
            "Simulate rule-based strategies on historical OHLCV data"
        ))

        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(16, 12, 16, 12); cv.setSpacing(8)

        # ── Config toolbar ────────────────────────────────────
        toolbar = self._build_toolbar()
        cv.addWidget(toolbar)

        # ── Progress bar ──────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setTextVisible(True)
        self._progress.setFixedHeight(16)
        self._progress.setVisible(False)
        self._progress.setStyleSheet(
            "QProgressBar { background:#0F1623; border:1px solid #1A2332; border-radius:3px; }"
            "QProgressBar::chunk { background:#1E90FF; border-radius:3px; }"
        )
        cv.addWidget(self._progress)

        # ── Main splitter: chart+metrics top, trade log bottom ─
        splitter = QSplitter(Qt.Vertical)

        # Top half: equity chart + metric cards
        top_widget = QWidget()
        top_layout = QHBoxLayout(top_widget)
        top_layout.setContentsMargins(0, 0, 0, 0); top_layout.setSpacing(8)

        # Equity curve chart — custom axes for date labels and comma-formatted equity
        self._equity_chart = pg.PlotWidget(
            axisItems={
                "bottom": _DateAxisItem(orientation="bottom"),
                "left":   _CommaAxisItem(orientation="left"),
            }
        )
        self._equity_chart.setBackground(_C_BG)
        self._equity_chart.showGrid(x=True, y=True, alpha=0.15)
        self._equity_chart.getAxis("left").setTextPen(pg.mkPen(_C_MUTED))
        self._equity_chart.getAxis("bottom").setTextPen(pg.mkPen(_C_MUTED))
        self._equity_chart.setMinimumHeight(220)
        self._equity_chart.setLabel("left", "Equity (USDT)", color=_C_MUTED)
        top_layout.addWidget(self._equity_chart, 3)

        # Metric cards — 3 rows × 2 columns so each card is wide enough
        # for its value text without cramping.
        metrics_widget = QWidget()
        metrics_widget.setMinimumWidth(300)
        mg = QGridLayout(metrics_widget)
        mg.setContentsMargins(0, 0, 0, 0); mg.setSpacing(5)
        mg.setColumnStretch(0, 1); mg.setColumnStretch(1, 1)

        self._m_return   = MetricCard("TOTAL RETURN")
        self._m_dd       = MetricCard("MAX DRAWDOWN")
        self._m_sharpe   = MetricCard("SHARPE RATIO")
        self._m_winrate  = MetricCard("WIN RATE")
        self._m_trades   = MetricCard("TOTAL TRADES")
        self._m_pf       = MetricCard("PROFIT FACTOR")

        # Row 0: Total Return | Max Drawdown
        # Row 1: Sharpe Ratio | Win Rate
        # Row 2: Total Trades | Profit Factor
        for idx, card in enumerate((self._m_return, self._m_dd,
                                    self._m_sharpe, self._m_winrate,
                                    self._m_trades, self._m_pf)):
            mg.addWidget(card, idx // 2, idx % 2)

        top_layout.addWidget(metrics_widget)
        splitter.addWidget(top_widget)

        # Bottom: trade log table
        trade_widget = QWidget()
        tv = QVBoxLayout(trade_widget)
        tv.setContentsMargins(0, 4, 0, 0); tv.setSpacing(4)

        log_hdr = QHBoxLayout()
        log_hdr.addWidget(QLabel("Trade Log",
                                  styleSheet="font-weight:bold; color:#E8EBF0; font-size:12px;"))
        self._log_count = QLabel("")
        self._log_count.setStyleSheet("color:#8899AA; font-size:11px;")
        log_hdr.addWidget(self._log_count); log_hdr.addStretch()

        save_btn = QPushButton("Save Results")
        save_btn.setStyleSheet(_BTN); save_btn.setFixedHeight(26)
        save_btn.clicked.connect(self._save_results)
        log_hdr.addWidget(save_btn)
        tv.addLayout(log_hdr)

        self._trade_table = QTableWidget(0, 8)
        self._trade_table.setHorizontalHeaderLabels([
            "Entry Time", "Exit Time", "Entry Price",
            "Exit Price", "Qty", "P&L (USDT)", "P&L %", "Exit Reason"
        ])
        self._trade_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._trade_table.horizontalHeader().setStyleSheet(
            "QHeaderView::section { background:#0F1623; color:#8899AA; "
            "border:none; border-bottom:1px solid #1A2332; padding:4px; font-size:11px; }"
        )
        self._trade_table.setStyleSheet(
            "QTableWidget { background:#0A0E1A; color:#C0CCD8; border:none; "
            "gridline-color:#1A2332; font-size:11px; }"
            "QTableWidget::item:selected { background:#1A2D4A; }"
        )
        self._trade_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._trade_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._trade_table.setAlternatingRowColors(True)
        self._trade_table.setMinimumHeight(160)
        tv.addWidget(self._trade_table, 1)
        splitter.addWidget(trade_widget)

        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 1)
        cv.addWidget(splitter, 1)

        layout.addWidget(content, 1)

    # ── Toolbar ───────────────────────────────────────────────
    def _build_toolbar(self) -> QFrame:
        _DATE_SS = (
            "QDateEdit { background:#131B2A; color:#E8EBF0; border:1px solid #2A3A52; "
            "border-radius:4px; padding:2px 6px; font-size:12px; min-height:28px; }"
            "QDateEdit:focus { border-color:#1E90FF; }"
            "QDateEdit::drop-down { border:none; width:18px; }"
        )
        bar = QFrame(); bar.setObjectName("card")
        v = QVBoxLayout(bar); v.setContentsMargins(14, 10, 14, 10); v.setSpacing(6)

        # ── Row 1: strategy / symbol / TF / capital / buttons ──
        row1 = QHBoxLayout(); row1.setSpacing(10)

        # Strategy
        row1.addWidget(QLabel("Strategy:", styleSheet="color:#8899AA; font-size:11px; font-weight:600;"))
        self._strat_combo = QComboBox(); self._strat_combo.setStyleSheet(_COMBO)
        self._strat_combo.setMinimumWidth(200)
        self._strat_combo.currentIndexChanged.connect(self._clear_results)
        self._strat_combo.currentIndexChanged.connect(self._on_strategy_selected)
        row1.addWidget(self._strat_combo)

        # Auto-detected symbol + primary TF display (read-only, updated on strategy change)
        self._strat_info_lbl = QLabel("")
        self._strat_info_lbl.setStyleSheet(
            "color:#5A8AAA; font-size:11px; font-weight:600; "
            "background:#0D1C2E; border:1px solid #1E3048; border-radius:4px; "
            "padding:3px 10px;"
        )
        self._strat_info_lbl.setMinimumWidth(140)
        self._strat_info_lbl.setVisible(False)
        row1.addWidget(self._strat_info_lbl)

        row1.addSpacing(8)

        # Capital — uses _FormattedSpinBox for comma-formatted display (10,000)
        row1.addWidget(QLabel("Capital:", styleSheet="color:#8899AA; font-size:11px;"))
        self._capital_spin = _FormattedSpinBox(); self._capital_spin.setStyleSheet(_SPIN)
        self._capital_spin.setRange(100, 10_000_000); self._capital_spin.setValue(10_000)
        self._capital_spin.setDecimals(0); self._capital_spin.setSuffix(" USDT")
        self._capital_spin.setFixedWidth(140)
        row1.addWidget(self._capital_spin)

        row1.addSpacing(8)

        # Fee spinbox — pre-filled with KuCoin standard 0.10% maker & taker
        row1.addWidget(QLabel("Fee:", styleSheet="color:#8899AA; font-size:11px;"))
        self._fee_spin = QDoubleSpinBox()
        self._fee_spin.setStyleSheet(_SPIN)
        self._fee_spin.setRange(0.0, 2.0)
        self._fee_spin.setValue(0.10)          # KuCoin standard spot 0.10%
        self._fee_spin.setDecimals(2)
        self._fee_spin.setSuffix("% /side")
        self._fee_spin.setFixedWidth(110)
        self._fee_spin.setToolTip(
            "Exchange fee applied on BOTH entry and exit.\n"
            "KuCoin standard: 0.10% maker & 0.10% taker\n"
            "KuCoin VIP1: 0.08%   KuCoin VIP2: 0.06%"
        )
        row1.addWidget(self._fee_spin)

        row1.addStretch()

        # Run button
        self._run_btn = QPushButton("▶  Run Backtest")
        self._run_btn.setFixedHeight(34)
        self._run_btn.setStyleSheet(
            "QPushButton { background:#E87820; color:#FFF; border:none; border-radius:5px; "
            "font-size:13px; font-weight:700; padding:0 18px; }"
            "QPushButton:hover { background:#FF8C30; }"
            "QPushButton:disabled { background:#3A2A10; color:#886633; }"
        )
        self._run_btn.clicked.connect(self._run_backtest)
        row1.addWidget(self._run_btn)

        # Promote lifecycle button (hidden until backtest completes with trades)
        self._promote_btn = QPushButton("⬆  Promote Stage")
        self._promote_btn.setFixedHeight(34)
        self._promote_btn.setVisible(False)
        self._promote_btn.setStyleSheet(
            "QPushButton { background:#00AA66; color:#FFF; border:none; border-radius:5px; "
            "font-size:12px; font-weight:700; padding:0 14px; }"
            "QPushButton:hover { background:#00CC77; }"
        )
        self._promote_btn.clicked.connect(self._promote_strategy)
        row1.addWidget(self._promote_btn)

        # Optimize button — AI rewrites the strategy based on the backtest results
        self._optimize_btn = QPushButton("⚡  Optimize with AI")
        self._optimize_btn.setFixedHeight(34)
        self._optimize_btn.setVisible(False)
        self._optimize_btn.setToolTip(
            "Send the backtest results to AI for analysis.\n"
            "The AI will identify weaknesses and save an improved\n"
            "version of this strategy (e.g. '… — Optimized v1')."
        )
        self._optimize_btn.setStyleSheet(
            "QPushButton { background:#4A1A8A; color:#E0CCFF; border:1px solid #7A3ACA; "
            "border-radius:5px; font-size:12px; font-weight:700; padding:0 14px; }"
            "QPushButton:hover { background:#6A2ACA; border-color:#9A5AEA; color:#FFF; }"
            "QPushButton:disabled { background:#1A0A2A; color:#4A3A6A; border-color:#2A1A4A; }"
        )
        self._optimize_btn.clicked.connect(self._optimize_strategy)
        row1.addWidget(self._optimize_btn)

        v.addLayout(row1)

        # ── Row 2: date range + data-range info ───────────────
        row2 = QHBoxLayout(); row2.setSpacing(10)

        row2.addWidget(QLabel("From:", styleSheet="color:#8899AA; font-size:11px;"))
        self._start_date = QDateEdit()
        self._start_date.setStyleSheet(_DATE_SS)
        self._start_date.setCalendarPopup(True)
        self._start_date.setDisplayFormat("yyyy-MM-dd")
        # Default: 1 year ago
        self._start_date.setDate(QDate.currentDate().addYears(-1))
        self._start_date.setFixedWidth(115)
        row2.addWidget(self._start_date)

        row2.addWidget(QLabel("To:", styleSheet="color:#8899AA; font-size:11px;"))
        self._end_date = QDateEdit()
        self._end_date.setStyleSheet(_DATE_SS)
        self._end_date.setCalendarPopup(True)
        self._end_date.setDisplayFormat("yyyy-MM-dd")
        self._end_date.setDate(QDate.currentDate())
        self._end_date.setFixedWidth(115)
        row2.addWidget(self._end_date)

        row2.addSpacing(12)
        self._data_range_lbl = QLabel("← Load data in Chart Workspace or fetch from Binance ↓")
        self._data_range_lbl.setStyleSheet("color:#4A6A8A; font-size:11px; font-style:italic;")
        row2.addWidget(self._data_range_lbl)

        row2.addSpacing(8)

        # Binance fetch button — downloads free public data for the selected range
        self._fetch_btn = QPushButton("📥  Fetch from Binance")
        self._fetch_btn.setFixedHeight(28)
        self._fetch_btn.setToolTip(
            "Download free historical OHLCV data from Binance's public API\n"
            "(no account needed — data back to 2017)\n"
            "Uses the symbol, timeframe, and date range selected above."
        )
        self._fetch_btn.setStyleSheet(
            "QPushButton { background:#0D2A1A; color:#00CC77; border:1px solid #00AA66; "
            "border-radius:4px; font-size:11px; font-weight:600; padding:0 12px; }"
            "QPushButton:hover { background:#0F3A24; border-color:#00CC77; }"
            "QPushButton:disabled { background:#0A1A10; color:#2A5A3A; border-color:#1A3A2A; }"
        )
        self._fetch_btn.clicked.connect(self._fetch_from_binance)
        row2.addWidget(self._fetch_btn)

        row2.addStretch()

        v.addLayout(row2)

        # ── Row 3: IDSS Pipeline quick-run ────────────────────
        sep = QFrame(); sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet("QFrame { color:#1A2332; }")
        v.addWidget(sep)

        row3 = QHBoxLayout(); row3.setSpacing(10)

        idss_lbl = QLabel("IDSS Pipeline:")
        idss_lbl.setStyleSheet(
            "color:#7B9EC0; font-size:11px; font-weight:700;"
        )
        idss_lbl.setToolTip(
            "Run the live IDSS signal pipeline (RegimeClassifier → SignalGenerator\n"
            "→ ConfluenceScorer → RiskGate) on historical data, bar-by-bar.\n"
            "Reuses Capital, Fee, and Date Range from above."
        )
        row3.addWidget(idss_lbl)

        row3.addWidget(QLabel("Symbol:", styleSheet="color:#8899AA; font-size:11px;"))
        self._idss_symbol_combo = QComboBox(); self._idss_symbol_combo.setStyleSheet(_COMBO)
        self._idss_symbol_combo.setMinimumWidth(130)
        self._idss_symbol_combo.setToolTip("Select the asset to backtest the IDSS pipeline on.")
        row3.addWidget(self._idss_symbol_combo)

        row3.addWidget(QLabel("TF:", styleSheet="color:#8899AA; font-size:11px;"))
        self._idss_tf_combo = QComboBox(); self._idss_tf_combo.setStyleSheet(_COMBO)
        self._idss_tf_combo.setFixedWidth(80)
        for tf in ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d"]:
            self._idss_tf_combo.addItem(tf, tf)
        self._idss_tf_combo.setCurrentText("1h")
        row3.addWidget(self._idss_tf_combo)

        row3.addStretch()

        self._idss_run_btn = QPushButton("▲  IDSS Backtest")
        self._idss_run_btn.setFixedHeight(34)
        self._idss_run_btn.setToolTip(
            "Replay the full IDSS signal pipeline on historical data.\n"
            "Uses the Capital, Fee, and Date Range configured above.\n"
            "No stored strategy needed — IDSS generates entries from its own models."
        )
        self._idss_run_btn.setStyleSheet(
            "QPushButton { background:#1A3060; color:#7FB3FF; border:1px solid #2A50A0; "
            "border-radius:5px; font-size:13px; font-weight:700; padding:0 18px; }"
            "QPushButton:hover { background:#2A4A90; border-color:#4A80D0; color:#C0DAFF; }"
            "QPushButton:disabled { background:#0A1A30; color:#2A4A70; border-color:#152040; }"
        )
        self._idss_run_btn.clicked.connect(self._run_idss_backtest)
        row3.addWidget(self._idss_run_btn)

        v.addLayout(row3)

        # ── Status strip ──────────────────────────────────────
        self._status_lbl = QLabel("← Select a strategy and click Run Backtest")
        self._status_lbl.setStyleSheet("color:#5A7A9A; font-size:11px;")
        v.addWidget(self._status_lbl)
        return bar

    # ── Data helpers ──────────────────────────────────────────
    def _load_strategies(self):
        try:
            from core.database.engine import get_session
            from core.database.models import Strategy, STRATEGY_TYPE_META
            with get_session() as s:
                rows = s.query(Strategy).order_by(Strategy.name).all()
                strats = [
                    {
                        "id":              r.id,
                        "name":            r.name,
                        "type":            r.type or "rule",
                        "lifecycle_stage": r.lifecycle_stage or 1,
                        "ai_generated":    r.ai_generated or False,
                        "definition":      r.definition or {},
                    }
                    for r in rows
                ]
        except Exception:
            strats = []

        current = self._strat_combo.currentData()
        self._strat_combo.blockSignals(True)
        self._strat_combo.clear()
        self._strat_combo.addItem("— select a strategy —", None)
        for st in strats:
            from core.database.models import STRATEGY_TYPE_META
            tag   = STRATEGY_TYPE_META.get(st["type"], ("?", "#888"))[0]
            label = f"[{tag}]  {st['name']}"
            self._strat_combo.addItem(label, st)
        if current:
            for i in range(self._strat_combo.count()):
                d = self._strat_combo.itemData(i)
                if d and d.get("id") == current.get("id"):
                    self._strat_combo.setCurrentIndex(i)
                    break
        self._strat_combo.blockSignals(False)

    def _load_symbols(self):
        # Symbol is now auto-detected from the strategy definition — no combo to populate.
        pass

    # ── Run backtest ──────────────────────────────────────────
    def _run_backtest(self):
        strategy = self._strat_combo.currentData()
        if not strategy:
            self._status_lbl.setText("⚠ Select a strategy first.")
            return

        symbol    = self._get_strategy_symbol(strategy)
        timeframe = self._get_strategy_primary_tf(strategy)
        capital   = self._capital_spin.value()

        if not symbol:
            self._status_lbl.setText("⚠ Could not detect symbol from strategy.")
            return

        # Resolve date range from pickers
        sd = self._start_date.date()
        ed = self._end_date.date()
        start_dt = datetime(sd.year(), sd.month(), sd.day())
        end_dt   = datetime(ed.year(), ed.month(), ed.day(), 23, 59, 59)

        if start_dt >= end_dt:
            self._status_lbl.setText("⚠ Start date must be before End date.")
            return

        # Load OHLCV data from DB for the selected date range
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            from core.market_data.historical_loader import load_ohlcv_from_db
            with get_session() as s:
                asset = s.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None
            if not asset_id:
                self._status_lbl.setText(
                    f"⚠ No data for {symbol}. Load it in Chart Workspace first.")
                return
            df = load_ohlcv_from_db(
                asset_id, timeframe,
                start_date=start_dt, end_date=end_dt,
            )
            if df is None or df.empty:
                # Fallback: try without date filter and show what's available
                df_all = load_ohlcv_from_db(asset_id, timeframe, limit=5000)
                if df_all is not None and not df_all.empty:
                    earliest = df_all.index[0].strftime("%Y-%m-%d")
                    latest   = df_all.index[-1].strftime("%Y-%m-%d")
                    self._data_range_lbl.setText(
                        f"Available: {earliest} → {latest}  ({len(df_all):,} candles)"
                    )
                    self._status_lbl.setText(
                        f"⚠ No {timeframe} data for {symbol} in selected range. "
                        f"Available: {earliest} → {latest}"
                    )
                else:
                    self._status_lbl.setText(
                        f"⚠ No {timeframe} data for {symbol}. Load it in Chart Workspace first.")
                return

            # Update data range label
            earliest = df.index[0].strftime("%Y-%m-%d")
            latest   = df.index[-1].strftime("%Y-%m-%d")
            days     = (df.index[-1] - df.index[0]).days
            self._data_range_lbl.setText(
                f"Data: {earliest} → {latest}  |  {len(df):,} candles  |  ~{days} days"
            )

        except Exception as e:
            self._status_lbl.setText(f"⚠ DB error: {e}")
            return

        # Stop any running worker
        if self._worker and self._worker.isRunning():
            self._worker.terminate()
            self._worker.wait(1000)

        fee_pct = self._fee_spin.value()
        config = {
            "symbol":          symbol,
            "timeframe":       timeframe,
            "initial_capital": capital,
            "fee_pct":         fee_pct,
        }

        # ── Multi-TF: load secondary timeframes from DB ───────
        extra_tfs = self._collect_strategy_timeframes(strategy)
        extra_tfs.discard(timeframe)   # primary TF already loaded

        df_map = {timeframe: df}       # always include the primary TF

        for extra_tf in sorted(extra_tfs, key=lambda t: ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"].index(t) if t in ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"] else 99):
            try:
                extra_df = load_ohlcv_from_db(
                    asset_id, extra_tf,
                    start_date=start_dt, end_date=end_dt,
                )
                if extra_df is not None and not extra_df.empty:
                    df_map[extra_tf] = extra_df
                    logger.info("_run_backtest: loaded %d bars for TF '%s'",
                                len(extra_df), extra_tf)
                else:
                    logger.warning("_run_backtest: no DB data for TF '%s' — "
                                   "fetch it from Binance first", extra_tf)
            except Exception as load_err:
                logger.warning("_run_backtest: could not load TF '%s': %s", extra_tf, load_err)

        # Store loaded TF list for display in status after run
        _TF_ORDER = ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"]
        config["loaded_timeframes"] = sorted(
            df_map.keys(),
            key=lambda t: _TF_ORDER.index(t) if t in _TF_ORDER else 99,
        )

        missing_tfs = sorted(extra_tfs - set(df_map.keys()))
        if missing_tfs:
            self._status_lbl.setText(
                f"⚠ Missing data for: {', '.join(missing_tfs)}.  "
                "Click 'Fetch from Binance' to download all required timeframes."
            )

        self._worker = BacktestWorker(strategy, df_map, timeframe, config)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)

        self._run_btn.setEnabled(False)
        self._promote_btn.setVisible(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status_lbl.setText(
            f"Running backtest for {strategy['name']} on {symbol} "
            f"({earliest} → {latest})…"
        )
        self._worker.start()

    @Slot(str)
    def _on_progress(self, msg: str):
        self._status_lbl.setText(msg)

    @Slot(dict)
    def _on_finished(self, result: dict):
        self._progress.setRange(0, 100); self._progress.setValue(100)
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._current_result = result
        self._render_results(result)

        # Show promote + optimize buttons only when backtest produced trades
        trade_count = len(result.get("trades", []))
        strat = self._strat_combo.currentData()
        if strat and strat.get("id") and trade_count > 0:
            from core.database.models import LIFECYCLE_STAGE_LABELS
            stage      = strat.get("lifecycle_stage", 1)
            next_stage = min(stage + 1, 6)
            next_label = LIFECYCLE_STAGE_LABELS.get(next_stage, ("Stage ?", "#888"))[0]
            self._promote_btn.setText(f"  Promote to Stage {next_stage}: {next_label}  ")
            self._promote_btn.setVisible(stage < 6)
            self._optimize_btn.setVisible(True)
            self._optimize_btn.setEnabled(True)
        else:
            self._promote_btn.setVisible(False)
            self._optimize_btn.setVisible(False)
            if trade_count == 0:
                self._status_lbl.setText(
                    self._status_lbl.text()
                    + "  ⚠ 0 trades — conditions never triggered in this date range. "
                    "Try a wider date range or check strategy conditions."
                )

        # Warn about any parse issues (AI strategies)
        warnings = result.get("parse_warnings", [])
        if warnings:
            self._status_lbl.setText(
                self._status_lbl.text()
                + f"  ⚠ {len(warnings)} condition(s) could not be parsed — check logs."
            )

    @Slot(str)
    def _on_error(self, err: str):
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._promote_btn.setVisible(False)
        self._optimize_btn.setVisible(False)
        self._status_lbl.setText(f"⚠ Error: {err}")

    # ── Render results ────────────────────────────────────────
    def _render_results(self, result: dict):
        metrics   = result.get("metrics", {})
        trades    = result.get("trades", [])
        eq_curve  = result.get("equity_curve", [])
        symbol    = result.get("symbol", "")
        tf        = result.get("timeframe", "")
        n         = result.get("candle_count", 0)
        name      = result.get("strategy_name", "")
        loaded_tfs = result.get("loaded_timeframes") or [tf]

        # ── Equity curve ──────────────────────────────────────
        self._equity_chart.clear()

        # Prefer per-bar mark-to-market data; fall back to sparse equity_curve
        chart_equity = result.get("chart_equity") or []
        ts_list      = result.get("equity_timestamps") or []
        plot_data    = chart_equity if chart_equity else eq_curve

        if plot_data:
            # Convert timestamps → Unix seconds for the DateAxisItem
            if ts_list and len(ts_list) == len(plot_data):
                try:
                    import pandas as pd
                    xs = np.array(
                        [pd.Timestamp(t).timestamp() for t in ts_list],
                        dtype=float,
                    )
                except Exception:
                    xs = np.arange(len(plot_data), dtype=float)
            else:
                xs = np.arange(len(plot_data), dtype=float)

            ys   = np.array(plot_data, dtype=float)
            init = float(result.get("initial_capital", ys[0]))

            bull_pen = pg.mkPen(_C_BULL, width=2)
            bear_pen = pg.mkPen(_C_BEAR, width=2)

            # Split into profit / loss segments for colour coding
            profit_xs, profit_ys = [], []
            loss_xs, loss_ys     = [], []
            for x, y in zip(xs, ys):
                if y >= init:
                    profit_xs.append(x); profit_ys.append(y)
                else:
                    loss_xs.append(x); loss_ys.append(y)

            if profit_xs:
                self._equity_chart.plot(profit_xs, profit_ys, pen=bull_pen)
            if loss_xs:
                self._equity_chart.plot(loss_xs, loss_ys, pen=bear_pen)

            # Baseline (initial capital)
            baseline_pen = pg.mkPen("#2A3A52", width=1, style=Qt.DashLine)
            self._equity_chart.addItem(
                pg.InfiniteLine(pos=init, angle=0, pen=baseline_pen))

        # ── Metric cards ──────────────────────────────────────
        ret = metrics.get("total_return_pct", 0.0)
        dd  = metrics.get("max_drawdown_pct", 0.0)
        sr  = metrics.get("sharpe_ratio", 0.0)
        wr  = metrics.get("win_rate", 0.0)
        nt  = metrics.get("total_trades", 0)
        pf  = metrics.get("profit_factor", 0.0)

        ret_color = _C_BULL if ret >= 0 else _C_BEAR
        dd_color  = _C_BEAR
        sr_color  = _C_BULL if sr >= 1 else (_C_MUTED if sr >= 0 else _C_BEAR)

        self._m_return.set_value(f"{ret:+.1f}%", ret_color)
        self._m_dd.set_value(f"-{dd:.1f}%", dd_color)
        self._m_sharpe.set_value(f"{sr:.2f}", sr_color)
        self._m_winrate.set_value(f"{wr:.1f}%",
                                   _C_BULL if wr >= 50 else _C_BEAR)
        self._m_trades.set_value(str(nt))
        self._m_pf.set_value(f"{pf:.2f}", _C_BULL if pf >= 1 else _C_BEAR)

        # ── Trade log table ───────────────────────────────────
        self._trade_table.setRowCount(0)
        for t in trades:
            row = self._trade_table.rowCount()
            self._trade_table.insertRow(row)
            pnl     = t.get("pnl", 0.0)
            pnl_pct = t.get("pnl_pct", 0.0)
            color   = QColor(_C_BULL) if pnl >= 0 else QColor(_C_BEAR)
            cells = [
                t.get("entry_time", "")[:16],
                t.get("exit_time",  "")[:16],
                f"{t.get('entry_price', 0):.6g}",
                f"{t.get('exit_price',  0):.6g}",
                f"{t.get('quantity', 0):.6g}",
                f"{pnl:+.4f}",
                f"{pnl_pct:+.2f}%",
                t.get("exit_reason", ""),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignCenter)
                if col in (5, 6):
                    item.setForeground(QBrush(color))
                self._trade_table.setItem(row, col, item)

        self._log_count.setText(
            f"{len(trades)} trades  |  "
            f"Win: {metrics.get('winning_trades', 0)}  "
            f"Loss: {metrics.get('losing_trades', 0)}"
        )
        tf_label = " + ".join(loaded_tfs) if loaded_tfs else tf
        self._status_lbl.setText(
            f"✓  {name}  ·  {symbol} [{tf_label}]  ·  {n:,} candles  ·  "
            f"{len(trades)} trades"
        )

    # ── Lifecycle promotion ───────────────────────────────────
    def _promote_strategy(self):
        strat = self._strat_combo.currentData()
        if not strat or not strat.get("id"):
            return
        try:
            from core.database.models import (
                promote_strategy_lifecycle, LIFECYCLE_STAGE_LABELS
            )
            new_stage = promote_strategy_lifecycle(strat["id"])
            stage_name = LIFECYCLE_STAGE_LABELS.get(new_stage, ("Stage ?", "#888"))[0]

            # Update local data so button re-labels correctly next time
            strat["lifecycle_stage"] = new_stage

            self._status_lbl.setText(
                f"✓  '{strat['name']}' promoted to Stage {new_stage}: {stage_name}"
            )

            # Hide button if now at max stage
            if new_stage >= 6:
                self._promote_btn.setVisible(False)
            else:
                next_stage = new_stage + 1
                next_name  = LIFECYCLE_STAGE_LABELS.get(next_stage, ("Stage ?", "#888"))[0]
                self._promote_btn.setText(f"  Promote to Stage {next_stage}: {next_name}  ")

            # Refresh strategy dropdown to reflect new stage label
            self._load_strategies()
        except Exception as exc:
            self._status_lbl.setText(f"⚠ Promote failed: {exc}")

    # ── AI strategy optimization ──────────────────────────────
    def _optimize_strategy(self):
        """Launch the AI optimizer against the current backtest result."""
        if not self._current_result:
            return
        strategy = self._strat_combo.currentData()
        if not strategy:
            return

        # Stop any existing optimize worker
        if self._optimize_worker and self._optimize_worker.isRunning():
            self._optimize_worker.terminate()
            self._optimize_worker.wait(1000)

        self._optimize_btn.setEnabled(False)
        self._run_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status_lbl.setText("🤖 AI is analyzing your backtest and optimizing the strategy…")

        self._optimize_worker = OptimizeWorker(strategy, self._current_result, self)
        self._optimize_worker.progress.connect(self._on_optimize_progress)
        self._optimize_worker.finished.connect(self._on_optimize_finished)
        self._optimize_worker.error.connect(self._on_optimize_error)
        self._optimize_worker.start()

    @Slot(str)
    def _on_optimize_progress(self, msg: str):
        self._status_lbl.setText(msg)

    @Slot(dict, str)
    def _on_optimize_finished(self, new_strategy: dict, changes: str):
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._optimize_btn.setEnabled(True)

        # Reload strategy dropdown so the new strategy appears immediately
        self._load_strategies()

        self._status_lbl.setText(
            f"✓  Optimized strategy saved: '{new_strategy['name']}'"
        )

        # Show the change-log dialog
        dlg = OptimizeResultDialog(new_strategy, changes, self)
        dlg.backtest_requested.connect(self._on_backtest_optimized_requested)
        dlg.exec()

    @Slot(dict)
    def _on_backtest_optimized_requested(self, new_strategy: dict):
        """Select the newly optimized strategy in the combo and run backtest."""
        # Find and select the new strategy in the combo box
        for i in range(self._strat_combo.count()):
            d = self._strat_combo.itemData(i)
            if d and d.get("id") == new_strategy.get("id"):
                self._strat_combo.setCurrentIndex(i)
                break
        # Trigger backtest automatically
        self._run_backtest()

    @Slot(str)
    def _on_optimize_error(self, err: str):
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setVisible(False)
        self._run_btn.setEnabled(True)
        self._optimize_btn.setEnabled(True)
        self._status_lbl.setText(f"⚠ Optimization failed: {err}")
        QMessageBox.warning(
            self, "Optimization Error",
            f"The AI optimizer encountered an error:\n\n{err}"
        )

    # ── Save results to DB ────────────────────────────────────
    def _save_results(self):
        if not self._current_result:
            return
        try:
            result = self._current_result
            metrics = result.get("metrics", {})
            strat   = self._strat_combo.currentData() or {}

            from core.database.engine import get_session
            from core.database.models import BacktestResult
            with get_session() as s:
                obj = BacktestResult(
                    strategy_id=strat.get("id"),
                    strategy_name=result.get("strategy_name", ""),
                    symbol=result.get("symbol", ""),
                    timeframe=result.get("timeframe", ""),
                    initial_capital=result.get("initial_capital", 10000.0),
                    final_capital=result.get("equity_curve", [10000.0])[-1],
                    total_return_pct=metrics.get("total_return_pct"),
                    max_drawdown_pct=metrics.get("max_drawdown_pct"),
                    sharpe_ratio=metrics.get("sharpe_ratio"),
                    win_rate=metrics.get("win_rate"),
                    total_trades=metrics.get("total_trades", 0),
                    profit_factor=metrics.get("profit_factor"),
                    run_config=strat.get("definition"),
                    equity_curve=result.get("equity_curve"),
                    trade_log=result.get("trades"),
                )
                s.add(obj)
            self._status_lbl.setText("✓ Results saved to database")
        except Exception as e:
            self._status_lbl.setText(f"⚠ Save error: {e}")

    # ── Strategy timeframe detection ──────────────────────────
    _TF_PATTERN = None   # compiled lazily

    @staticmethod
    def _collect_strategy_timeframes(strategy: dict) -> set:
        """
        Return the set of all non-primary timeframes referenced by a strategy.

        Handles both formats:
        - Rule-Builder strategies: walks ``entry/exit rule_tree`` nodes that
          carry an explicit ``"timeframe"`` field.
        - AI-generated strategies: scans the ``indicators`` array for per-
          indicator timeframe fields, the ``timeframes`` list if present, and
          every condition text string for inline TF tokens (e.g. "5m RSI > 30").
        """
        import re
        try:
            from core.backtesting.backtest_engine import _collect_timeframes
            defn  = strategy.get("definition") or {}
            tfs: set = set()

            # ── Rule-Builder: walk rule_tree nodes ────────────
            entry = defn.get("entry", {})
            exit_ = defn.get("exit",  {})
            for tree in (entry.get("rule_tree"), exit_.get("rule_tree")):
                if tree:
                    tfs |= _collect_timeframes(tree)

            # ── AI strategy: explicit "timeframes" list ───────
            for tf in defn.get("timeframes", []):
                if tf:
                    tfs.add(str(tf).strip())

            # ── AI strategy: per-indicator timeframe field ────
            for ind in defn.get("indicators", []):
                tf = (ind.get("timeframe") or "").strip()
                if tf:
                    tfs.add(tf)

            # ── AI strategy: inline TF tokens in condition text
            _valid = {"1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"}
            _tf_re = re.compile(r'\b(1m|3m|5m|15m|30m|1h|2h|4h|6h|12h|1d|1w)\b')
            for section in ("entry_long","exit_long","entry_short","exit_short"):
                for cond in defn.get(section, {}).get("conditions", []):
                    for match in _tf_re.findall(str(cond)):
                        tfs.add(match)

            # Only return TFs that differ from the strategy's own primary TF
            primary = (defn.get("timeframe") or "").strip()
            tfs.discard(primary)
            return tfs & _valid

        except Exception:
            return set()

    # ── Binance historical data fetch ─────────────────────────
    def _fetch_from_binance(self):
        """
        Download free OHLCV data from Binance for the selected range.

        Auto-detects all timeframes referenced by the currently selected
        strategy and fetches them all in one pass via BinanceMultiTFWorker.
        """
        strategy  = self._strat_combo.currentData()
        if not strategy:
            self._status_lbl.setText("⚠ Select a strategy first.")
            return
        symbol    = self._get_strategy_symbol(strategy)
        timeframe = self._get_strategy_primary_tf(strategy)

        if not symbol:
            self._status_lbl.setText("⚠ Could not detect symbol from strategy.")
            return

        sd = self._start_date.date()
        ed = self._end_date.date()
        start_dt = datetime(sd.year(), sd.month(), sd.day())
        end_dt   = datetime(ed.year(), ed.month(), ed.day(), 23, 59, 59)

        if start_dt >= end_dt:
            self._status_lbl.setText("⚠ Start date must be before End date.")
            return

        # Collect extra TFs needed by the selected strategy
        extra_tfs = self._collect_strategy_timeframes(strategy)
        extra_tfs.discard(timeframe)          # primary TF already included

        # Build ordered list: primary first, then secondary TFs sorted ascending
        _TF_ORDER = ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"]
        tfs_to_fetch = [timeframe] + sorted(
            extra_tfs,
            key=lambda t: _TF_ORDER.index(t) if t in _TF_ORDER else 99,
        )

        # Stop any running fetch worker
        if self._fetch_worker and self._fetch_worker.isRunning():
            self._fetch_worker.terminate()
            self._fetch_worker.wait(1000)

        from core.market_data.historical_loader import BinanceMultiTFWorker
        self._fetch_worker = BinanceMultiTFWorker(symbol, tfs_to_fetch, start_dt, end_dt)
        self._fetch_worker.progress.connect(self._on_fetch_progress)
        self._fetch_worker.finished.connect(self._on_fetch_finished_multi)
        self._fetch_worker.error.connect(self._on_fetch_error)

        self._fetch_btn.setEnabled(False)
        self._run_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._data_range_lbl.setText("Connecting to Binance…")

        tf_label = (f"{', '.join(tfs_to_fetch)} timeframes"
                    if len(tfs_to_fetch) > 1 else timeframe)
        self._status_lbl.setText(
            f"Fetching {symbol} [{tf_label}] from Binance "
            f"({start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')})…"
        )
        self._fetch_worker.start()

    @Slot(int, int, str)
    def _on_fetch_progress(self, current: int, total: int, msg: str):
        if total > 0:
            self._progress.setRange(0, total)
            self._progress.setValue(current)
        self._status_lbl.setText(msg)

    @Slot(str, dict)
    def _on_fetch_finished_multi(self, symbol: str, results: dict):
        """Handle completion of BinanceMultiTFWorker (multi-TF fetch)."""
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._progress.setVisible(False)
        self._fetch_btn.setEnabled(True)
        self._run_btn.setEnabled(True)

        # Refresh data range label for the primary TF
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            from core.market_data.historical_loader import load_ohlcv_from_db
            sd = self._start_date.date()
            ed = self._end_date.date()
            start_dt = datetime(sd.year(), sd.month(), sd.day())
            end_dt   = datetime(ed.year(), ed.month(), ed.day(), 23, 59, 59)
            _strat = self._strat_combo.currentData()
            tf = self._get_strategy_primary_tf(_strat) if _strat else "1h"
            with get_session() as s:
                asset    = s.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None
            if asset_id:
                df = load_ohlcv_from_db(asset_id, tf, start_date=start_dt, end_date=end_dt)
                if df is not None and not df.empty:
                    earliest = df.index[0].strftime("%Y-%m-%d")
                    latest   = df.index[-1].strftime("%Y-%m-%d")
                    days     = (df.index[-1] - df.index[0]).days
                    self._data_range_lbl.setText(
                        f"Data: {earliest} → {latest}  |  {len(df):,} candles  |  ~{days} days"
                    )
                else:
                    self._data_range_lbl.setText("Data saved — try widening the date range")
        except Exception:
            pass

        total_new = sum(v for v in results.values())
        tf_summary = "  ".join(
            f"{tf}: {n:,}" for tf, n in sorted(results.items())
        )
        verb = "new candles" if total_new > 0 else "candles (already up-to-date)"
        self._status_lbl.setText(
            f"✓  {total_new:,} {verb} for {symbol}  [{tf_summary}].  "
            f"Click ▶ Run Backtest."
        )
        self._load_symbols()

    @Slot(str, str)
    def _on_fetch_error(self, symbol: str, err: str):
        self._progress.setVisible(False)
        self._fetch_btn.setEnabled(True)
        self._run_btn.setEnabled(True)
        self._data_range_lbl.setText("← Fetch failed — see status below")
        self._status_lbl.setText(f"⚠ Binance fetch error: {err}")

    # ── Clear results when strategy selection changes ─────────
    def _clear_results(self):
        """Reset all result widgets when the selected strategy changes."""
        self._current_result = None
        self._promote_btn.setVisible(False)
        self._optimize_btn.setVisible(False)
        self._equity_chart.clear()
        self._trade_table.setRowCount(0)
        self._log_count.setText("")
        for card in (self._m_return, self._m_dd, self._m_sharpe,
                     self._m_winrate, self._m_trades, self._m_pf):
            card.set_value("—")
        self._status_lbl.setText("← Select a strategy and click Run Backtest")

    def _on_strategy_selected(self):
        """Update the strategy info label (symbol + primary TF) when strategy changes."""
        strategy = self._strat_combo.currentData()
        if not strategy:
            self._strat_info_lbl.setVisible(False)
            return
        sym = self._get_strategy_symbol(strategy)
        ptf = self._get_strategy_primary_tf(strategy)
        extra_tfs = self._collect_strategy_timeframes(strategy)
        extra_tfs.discard(ptf)
        _TF_ORDER = ["1m","3m","5m","15m","30m","1h","2h","4h","6h","12h","1d","1w"]
        all_tfs = [ptf] + sorted(
            extra_tfs,
            key=lambda t: _TF_ORDER.index(t) if t in _TF_ORDER else 99,
        )
        tf_str = " + ".join(all_tfs)
        self._strat_info_lbl.setText(f"{sym}  ·  {tf_str}")
        self._strat_info_lbl.setVisible(True)

    # ── Strategy symbol / primary TF auto-detection ───────────
    @staticmethod
    def _get_strategy_symbol(strategy: dict) -> str:
        """Extract the trading symbol from a strategy definition."""
        defn = strategy.get("definition") or {}
        # AI strategy: definition["symbols"] = ["BTC/USDT", ...]
        syms = defn.get("symbols")
        if syms and isinstance(syms, list) and syms[0]:
            return syms[0]
        # Rule strategy: definition["rule_tree"]["symbol"]
        tree = defn.get("rule_tree") or {}
        sym = (tree.get("symbol") or "").strip()
        if sym:
            return sym
        return "BTC/USDT"   # safe default

    @staticmethod
    def _get_strategy_primary_tf(strategy: dict) -> str:
        """Extract the primary (loop) timeframe from a strategy definition."""
        defn = strategy.get("definition") or {}
        # AI strategy stores a top-level "timeframe"
        tf = (defn.get("timeframe") or "").strip()
        if tf:
            return tf
        # Rule strategy may store "timeframe" inside rule_tree
        tree = defn.get("rule_tree") or {}
        tf = (tree.get("timeframe") or "").strip()
        if tf:
            return tf
        return "1h"   # fallback

    # ── IDSS backtest: symbol loader ──────────────────────────
    def _load_idss_symbols(self):
        """Populate the IDSS symbol combo from all assets stored in the DB."""
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            with get_session() as s:
                assets = s.query(Asset).order_by(Asset.symbol).all()
                symbols = [a.symbol for a in assets]
        except Exception:
            symbols = []

        current = self._idss_symbol_combo.currentText()
        self._idss_symbol_combo.blockSignals(True)
        self._idss_symbol_combo.clear()
        for sym in symbols:
            self._idss_symbol_combo.addItem(sym, sym)
        # Restore previous selection or default to BTC/USDT
        idx = self._idss_symbol_combo.findText(current)
        if idx >= 0:
            self._idss_symbol_combo.setCurrentIndex(idx)
        elif self._idss_symbol_combo.findText("BTC/USDT") >= 0:
            self._idss_symbol_combo.setCurrentText("BTC/USDT")
        self._idss_symbol_combo.blockSignals(False)

    # ── IDSS backtest: run ─────────────────────────────────────
    def _run_idss_backtest(self):
        """Load OHLCV data from DB and launch IDSSBacktestWorker."""
        symbol    = self._idss_symbol_combo.currentText()
        timeframe = self._idss_tf_combo.currentData() or "1h"
        capital   = self._capital_spin.value()
        fee_pct   = self._fee_spin.value()

        if not symbol:
            self._status_lbl.setText("⚠ No symbol selected. Add assets in Chart Workspace first.")
            return

        # Resolve date range from the shared pickers
        sd = self._start_date.date()
        ed = self._end_date.date()
        start_dt = datetime(sd.year(), sd.month(), sd.day())
        end_dt   = datetime(ed.year(), ed.month(), ed.day(), 23, 59, 59)

        if start_dt >= end_dt:
            self._status_lbl.setText("⚠ Start date must be before End date.")
            return

        # Load OHLCV from DB
        try:
            from core.database.engine import get_session
            from core.database.models import Asset
            from core.market_data.historical_loader import load_ohlcv_from_db

            with get_session() as s:
                asset    = s.query(Asset).filter_by(symbol=symbol).first()
                asset_id = asset.id if asset else None

            if not asset_id:
                self._status_lbl.setText(
                    f"⚠ No data for {symbol}. Fetch it from Binance first."
                )
                return

            df = load_ohlcv_from_db(
                asset_id, timeframe,
                start_date=start_dt, end_date=end_dt,
            )

            if df is None or df.empty:
                # Show what IS available
                df_all = load_ohlcv_from_db(asset_id, timeframe, limit=5000)
                if df_all is not None and not df_all.empty:
                    earliest = df_all.index[0].strftime("%Y-%m-%d")
                    latest   = df_all.index[-1].strftime("%Y-%m-%d")
                    self._status_lbl.setText(
                        f"⚠ No {timeframe} data for {symbol} in selected range. "
                        f"Available: {earliest} → {latest}"
                    )
                else:
                    self._status_lbl.setText(
                        f"⚠ No {timeframe} data for {symbol}. "
                        "Use 📥 Fetch from Binance first."
                    )
                return

            earliest = df.index[0].strftime("%Y-%m-%d")
            latest   = df.index[-1].strftime("%Y-%m-%d")
            days     = (df.index[-1] - df.index[0]).days
            self._data_range_lbl.setText(
                f"Data: {earliest} → {latest}  |  {len(df):,} candles  |  ~{days} days"
            )

        except Exception as exc:
            self._status_lbl.setText(f"⚠ DB error: {exc}")
            return

        # Stop any running worker
        if self._idss_worker and self._idss_worker.isRunning():
            self._idss_worker.terminate()
            self._idss_worker.wait(1000)

        config = {
            "initial_capital": capital,
            "fee_pct":         fee_pct,
            "slippage_pct":    0.05,
        }

        self._idss_worker = IDSSBacktestWorker(symbol, timeframe, df, config, self)
        self._idss_worker.progress.connect(self._on_idss_progress)
        self._idss_worker.finished.connect(self._on_idss_finished)
        self._idss_worker.error.connect(self._on_idss_error)

        self._idss_run_btn.setEnabled(False)
        self._run_btn.setEnabled(False)
        self._progress.setRange(0, 0)
        self._progress.setVisible(True)
        self._status_lbl.setText(
            f"Running IDSS backtest for {symbol} [{timeframe}] "
            f"({earliest} → {latest})…"
        )
        self._idss_worker.start()

    @Slot(str)
    def _on_idss_progress(self, msg: str):
        self._status_lbl.setText(msg)

    @Slot(dict)
    def _on_idss_finished(self, result: dict):
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._progress.setVisible(False)
        self._idss_run_btn.setEnabled(True)
        self._run_btn.setEnabled(True)
        self._current_result = result

        # IDSS results never have a stored strategy → hide promote/optimize
        self._promote_btn.setVisible(False)
        self._optimize_btn.setVisible(False)

        self._render_results(result)

        trade_count = len(result.get("trades", []))
        if trade_count == 0:
            self._status_lbl.setText(
                self._status_lbl.text()
                + "  ⚠ 0 trades — IDSS found no qualifying signals in this date range. "
                "Try a wider range or lower the confluence threshold in Settings."
            )

    @Slot(str)
    def _on_idss_error(self, err: str):
        self._progress.setVisible(False)
        self._idss_run_btn.setEnabled(True)
        self._run_btn.setEnabled(True)
        self._status_lbl.setText(f"⚠ IDSS Backtest error: {err}")

    def showEvent(self, event):
        super().showEvent(event)
        self._load_strategies()
        self._load_idss_symbols()
        # Refresh the info label for any previously selected strategy
        self._on_strategy_selected()
