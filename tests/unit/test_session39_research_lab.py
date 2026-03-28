"""
tests/unit/test_session39_research_lab.py
==========================================
Session 39 — Research Lab professional platform tests.

Covers:
  1.  DataRegistry file scan + metadata
  2.  DataRegistry JSON persistence (save / load round-trip)
  3.  DataRegistry.validate_period() — all-good path
  4.  DataRegistry.validate_period() — missing file path
  5.  DataRegistry.validate_period() — partial coverage path
  6.  DataRegistry.summary_table() shape + fields
  7.  DataRegistry.available_symbols() only returns fully-ready symbols
  8.  DataManager.check() — ready symbols pass
  9.  DataManager.check() — missing symbol surfaces in result
  10. _LabState date/symbol defaults
  11. _LabState accepts custom date/symbol
  12. SweepEngine __init__ stores date/symbols
  13. _init_worker signature accepts (date_start, date_end, symbols)
  14. _ParamRow spinbox width ≥ 75px  (layout fix)
  15. _ValidationPanel info labels present
  16. Baseline cache JSON structure
  17. Period selector date validation: start < end
  18. Asset selection: default symbols correct
  19. DataRegistry FileRecord.covers() logic
  20. DataManager.check() returns CheckResult dataclass fields
"""
from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# Guard: Qt widget tests require libEGL / display — skip in headless CI
import importlib.util as _ilu
_HAS_QT_DISPLAY = _ilu.find_spec("PySide6") is not None and (
    Path("/usr/lib/x86_64-linux-gnu/libEGL.so.1").exists()
    or Path("/usr/lib/libEGL.so.1").exists()
    or Path("/lib/x86_64-linux-gnu/libEGL.so.1").exists()
)
_qt_only = pytest.mark.skipif(
    not _HAS_QT_DISPLAY,
    reason="libEGL not available — Qt widget tests skipped in headless sandbox",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_fake_parquet(tmp_dir: Path, symbol: str, tf: str, n_rows: int = 100,
                        first: str = "2022-01-01", last: str = "2026-03-21") -> Path:
    """Write a minimal parquet file that DataRegistry can scan."""
    import pandas as pd
    import numpy as np
    slug = symbol.replace("/", "_")
    path = tmp_dir / f"{slug}_{tf}.parquet"
    idx  = pd.date_range(start=first, end=last, periods=n_rows, tz="UTC")
    df   = pd.DataFrame({
        "open":   np.ones(n_rows),
        "high":   np.ones(n_rows) * 1.01,
        "low":    np.ones(n_rows) * 0.99,
        "close":  np.ones(n_rows),
        "volume": np.ones(n_rows) * 1000,
    }, index=idx)
    df.to_parquet(path)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# 1. DataRegistry file scan + metadata
# ─────────────────────────────────────────────────────────────────────────────

def test_data_registry_scan_real_btc_30m():
    """BTC/USDT 30m parquet should exist in DATA_DIR and be scannable."""
    from research.engine.data_registry import DataRegistry, DATA_DIR
    reg = DataRegistry()
    # Only scan BTC to keep test fast
    reg.build(symbols=["BTC/USDT"])
    rec = reg.get("BTC/USDT", "30m")
    assert rec is not None
    assert rec.status == "ok"
    assert rec.rows > 0
    assert rec.first_date != ""
    assert rec.last_date  != ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. DataRegistry JSON persistence (save / load round-trip)
# ─────────────────────────────────────────────────────────────────────────────

def test_data_registry_save_load_roundtrip(tmp_path):
    """Save registry to tmp JSON and reload; metadata preserved."""
    from research.engine.data_registry import DataRegistry, _REGISTRY_PATH, FileRecord
    reg = DataRegistry()
    reg.build(symbols=["BTC/USDT"])
    rec_before = reg.get("BTC/USDT", "30m")
    assert rec_before is not None and rec_before.ok()

    # Patch the save path to tmp
    with patch("research.engine.data_registry._REGISTRY_PATH", tmp_path / "registry.json"):
        reg.save()
        reg2 = DataRegistry()
        with patch("research.engine.data_registry._REGISTRY_PATH", tmp_path / "registry.json"):
            ok = reg2.load()
    assert ok
    rec_after = reg2.get("BTC/USDT", "30m")
    assert rec_after is not None
    assert rec_after.rows == rec_before.rows
    assert rec_after.first_date == rec_before.first_date


# ─────────────────────────────────────────────────────────────────────────────
# 3. DataRegistry.validate_period() — all-good path
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_period_all_ok():
    """Symbols with full coverage produce no issues."""
    from research.engine.data_registry import DataRegistry
    reg = DataRegistry()
    reg.build(symbols=["BTC/USDT", "ETH/USDT"])
    issues = reg.validate_period(["BTC/USDT", "ETH/USDT"], "2022-04-01", "2026-03-01")
    assert issues == [], f"Unexpected issues: {issues}"


# ─────────────────────────────────────────────────────────────────────────────
# 4. DataRegistry.validate_period() — missing file path
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_period_missing_symbol():
    """Symbol not in registry produces an issue."""
    from research.engine.data_registry import DataRegistry
    reg = DataRegistry()
    # Do not build — registry is empty
    issues = reg.validate_period(["FAKE/USDT"], "2022-01-01", "2026-01-01")
    assert len(issues) == 1
    assert "FAKE/USDT" in issues[0]


# ─────────────────────────────────────────────────────────────────────────────
# 5. DataRegistry.validate_period() — partial coverage (synthetic)
# ─────────────────────────────────────────────────────────────────────────────

def test_validate_period_partial_coverage(tmp_path, monkeypatch):
    """Symbol with data only up to 2024-01-01 fails coverage check for 2026."""
    import pandas as pd
    from research.engine.data_registry import DataRegistry, REQUIRED_TFS

    monkeypatch.setattr("research.engine.data_registry.DATA_DIR", tmp_path)
    for tf in REQUIRED_TFS:
        _make_fake_parquet(tmp_path, "XRP/USDT", tf, n_rows=200,
                           first="2022-01-01", last="2024-01-01")

    reg = DataRegistry()
    reg.build(symbols=["XRP/USDT"])
    issues = reg.validate_period(["XRP/USDT"], "2022-01-01", "2026-01-01")
    assert len(issues) > 0
    assert "XRP/USDT" in issues[0]


# ─────────────────────────────────────────────────────────────────────────────
# 6. DataRegistry.summary_table() shape + fields
# ─────────────────────────────────────────────────────────────────────────────

def test_summary_table_structure():
    """summary_table() returns exactly one row per SUPPORTED_SYMBOLS."""
    from research.engine.data_registry import DataRegistry, SUPPORTED_SYMBOLS
    reg = DataRegistry()
    reg.build()
    rows = reg.summary_table()
    assert len(rows) == len(SUPPORTED_SYMBOLS)
    required_keys = {"symbol", "status", "first_date", "last_date", "rows_30m", "size_mb"}
    for row in rows:
        assert required_keys.issubset(row.keys())


# ─────────────────────────────────────────────────────────────────────────────
# 7. DataRegistry.available_symbols() only returns fully-ready symbols
# ─────────────────────────────────────────────────────────────────────────────

def test_available_symbols_subset():
    """All known symbols with parquets should appear in available_symbols()."""
    from research.engine.data_registry import DataRegistry, SUPPORTED_SYMBOLS
    reg = DataRegistry()
    reg.build()
    available = reg.available_symbols()
    # All 5 known symbols have data
    assert len(available) >= 3    # at minimum BTC, ETH, SOL
    assert all(s in SUPPORTED_SYMBOLS for s in available)


# ─────────────────────────────────────────────────────────────────────────────
# 8. DataManager.check() — ready symbols pass
# ─────────────────────────────────────────────────────────────────────────────

def test_data_manager_check_ready():
    """BTC/ETH with default dates should produce ok=True CheckResult."""
    from research.engine.data_manager import DataManager
    dm = DataManager()
    dm.refresh_registry()
    result = dm.check(["BTC/USDT", "ETH/USDT"], "2022-04-01", "2026-03-01")
    assert result.ok, f"Issues: {result.issues}"
    assert "BTC/USDT" in result.ready
    assert "ETH/USDT" in result.ready


# ─────────────────────────────────────────────────────────────────────────────
# 9. DataManager.check() — missing symbol surfaces in result
# ─────────────────────────────────────────────────────────────────────────────

def test_data_manager_check_missing_symbol():
    """A symbol with no data should appear in result.missing."""
    from research.engine.data_manager import DataManager
    dm = DataManager()
    dm.refresh_registry()
    result = dm.check(["FAKE/USDT"], "2022-01-01", "2026-01-01")
    assert not result.ok
    assert "FAKE/USDT" in result.missing


# ─────────────────────────────────────────────────────────────────────────────
# 10. _LabState date/symbol defaults
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_lab_state_defaults():
    """_LabState defaults match production backtest period and default symbols."""
    from gui.pages.research_lab.research_lab_page import _LabState
    state = _LabState()
    assert state.date_start == "2022-03-22"
    assert state.date_end   == "2026-03-21"
    assert "BTC/USDT" in state.selected_symbols
    assert len(state.selected_symbols) >= 3


# ─────────────────────────────────────────────────────────────────────────────
# 11. _LabState accepts custom date/symbol
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_lab_state_custom():
    """_LabState fields are mutable and reflect custom values."""
    from gui.pages.research_lab.research_lab_page import _LabState
    state = _LabState()
    state.date_start = "2023-01-01"
    state.date_end   = "2025-01-01"
    state.selected_symbols = ["BTC/USDT"]
    assert state.date_start == "2023-01-01"
    assert state.date_end   == "2025-01-01"
    assert state.selected_symbols == ["BTC/USDT"]


# ─────────────────────────────────────────────────────────────────────────────
# 12. SweepEngine __init__ stores date/symbols
# ─────────────────────────────────────────────────────────────────────────────

def test_sweep_engine_stores_period():
    """SweepEngine stores custom date_start, date_end, symbols."""
    from research.engine.sweep_engine import SweepEngine
    engine = SweepEngine(
        n_workers  = 1,
        date_start = "2023-01-01",
        date_end   = "2025-06-01",
        symbols    = ["BTC/USDT"],
    )
    assert engine.date_start == "2023-01-01"
    assert engine.date_end   == "2025-06-01"
    assert engine.symbols    == ["BTC/USDT"]


# ─────────────────────────────────────────────────────────────────────────────
# 13. _init_worker signature accepts (date_start, date_end, symbols)
# ─────────────────────────────────────────────────────────────────────────────

def test_init_worker_signature():
    """_init_worker must accept (date_start, date_end, symbols) kwargs."""
    import inspect
    from research.engine.sweep_engine import _init_worker
    sig = inspect.signature(_init_worker)
    params = list(sig.parameters.keys())
    assert "date_start" in params
    assert "date_end"   in params
    assert "symbols"    in params


# ─────────────────────────────────────────────────────────────────────────────
# 14. _ParamRow spinbox width ≥ 75px  (layout fix)
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_param_row_spinbox_width():
    """Spinbox must be at least 75px wide so '40.00' is not clipped."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from research.engine.parameter_registry import ALL_PARAMS
    from gui.pages.research_lab.research_lab_page import _ParamRow
    row = _ParamRow(ALL_PARAMS[0])
    assert row._spin.width() >= 75 or row._spin.minimumWidth() >= 75 or row._spin.fixedWidth() >= 75, \
        "Spinbox too narrow — values like '40.00' will be clipped"


# ─────────────────────────────────────────────────────────────────────────────
# 15. _ValidationPanel info labels present
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_validation_panel_info_labels():
    """Validation panel must show IS and OOS labels with proper content."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.pages.research_lab.research_lab_page import _ValidationPanel
    from PySide6.QtWidgets import QLabel
    vp = _ValidationPanel()
    labels = vp.findChildren(QLabel)
    texts = [lbl.text() for lbl in labels]
    combined = " ".join(texts)
    assert "IS period" in combined or "2022-03-22" in combined
    assert "OOS period" in combined or "2025-09-22" in combined


# ─────────────────────────────────────────────────────────────────────────────
# 16. Baseline cache JSON structure
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_baseline_cache_json_structure(tmp_path):
    """Baseline cache must have 'saved_at' and 'summary' keys."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.pages.research_lab.research_lab_page import ResearchLabPage
    import time

    cache_path = tmp_path / "last_baseline_result.json"
    with patch("gui.pages.research_lab.research_lab_page._BASELINE_CACHE", cache_path):
        page = ResearchLabPage()
        summary = {"passed": True, "r0": {"profit_factor": 1.37}, "r1": {}, "failures": []}
        page._save_baseline_cache(summary)

    assert cache_path.exists()
    data = json.loads(cache_path.read_text())
    assert "saved_at" in data
    assert "summary"  in data
    assert data["summary"]["passed"] is True


# ─────────────────────────────────────────────────────────────────────────────
# 17. Period selector date validation: start < end
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_period_panel_validation():
    """_PeriodPanel.is_valid() returns False when start >= end."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QDate
    app = QApplication.instance() or QApplication([])
    from gui.pages.research_lab.research_lab_page import _PeriodPanel
    panel = _PeriodPanel()
    # Default: start 2022-03-22, end 2026-03-21 → valid
    assert panel.is_valid()

    # Set start after end → invalid
    panel._start_edit.setDate(QDate(2027, 1, 1))
    assert not panel.is_valid()


# ─────────────────────────────────────────────────────────────────────────────
# 18. Asset selection: default symbols correct
# ─────────────────────────────────────────────────────────────────────────────

@_qt_only
def test_asset_panel_defaults():
    """_AssetPanel default selection matches _DEFAULT_SYMBOLS."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from gui.pages.research_lab.research_lab_page import _AssetPanel, _DEFAULT_SYMBOLS
    panel = _AssetPanel()
    sel   = panel.selected_symbols()
    assert sorted(sel) == sorted(_DEFAULT_SYMBOLS)


# ─────────────────────────────────────────────────────────────────────────────
# 19. DataRegistry FileRecord.covers() logic
# ─────────────────────────────────────────────────────────────────────────────

def test_file_record_covers():
    """FileRecord.covers() returns True iff dates fall within record range."""
    from research.engine.data_registry import FileRecord
    rec = FileRecord("BTC/USDT", "30m", "BTC_USDT_30m.parquet", "ok",
                     first_date="2022-01-01", last_date="2026-03-21")
    assert rec.covers("2022-03-22", "2026-03-21")
    assert not rec.covers("2021-01-01", "2026-03-21")   # start before first_date
    assert not rec.covers("2022-01-01", "2027-01-01")   # end after last_date


# ─────────────────────────────────────────────────────────────────────────────
# 20. DataManager.check() returns CheckResult dataclass fields
# ─────────────────────────────────────────────────────────────────────────────

def test_check_result_fields():
    """CheckResult has ok, ready, missing, partial, issues fields."""
    from research.engine.data_manager import CheckResult
    result = CheckResult(ok=True, ready=["BTC/USDT"], missing=[], partial=[], issues=[])
    assert result.ok
    assert result.summary().startswith("All")
    d = asdict(result)
    assert set(d.keys()) == {"ok", "ready", "missing", "partial", "issues"}
