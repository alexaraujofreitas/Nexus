# Phase 1C — Dependency & Tooling Audit

**Date:** 2026-04-06
**Phase:** 1C of 9
**Status:** Complete

---

## 1. Current Dependencies vs Intraday Requirements

### 1.1 Dependencies That Stay Unchanged

| Package | Current Version | Used By | Intraday Impact |
|---|---|---|---|
| `pandas>=2.1.0` | Data processing | All modules | No change. 1m data increases row count ~30× per symbol but pandas handles this fine. |
| `numpy>=1.26.0` | Numerical computation | Indicators, regime, RL | No change. |
| `ta>=0.11.0` | Technical indicators | `indicator_library.py` | No change. All indicators (EMA, ADX, RSI, BB, ATR, MACD) work on any timeframe. |
| `scikit-learn>=1.3.0` | HMM dependency | `hmmlearn` | No change. |
| `hmmlearn>=0.3.0` | HMM regime classifier | `hmm_classifier.py` | Retrain on 5m data. Library itself unchanged. |
| `arch>=5.3.0` | MS-GARCH | `ms_garch_forecaster.py` | Refit on 5m data. Library unchanged. |
| `SQLAlchemy>=2.0.0` | ORM/DB | All database modules | No change. Add columns via migration. |
| `cryptography>=41.0.0` | API key vault | `key_vault.py` | No change. |
| `pyyaml>=6.0` | Config parsing | `config/settings.py` | No change. Larger config.yaml but trivial. |
| `requests>=2.31.0` | HTTP client | Agents, news_feed | No change. |
| `feedparser>=6.0.0` | RSS parsing | `news_feed.py` | No change (agent context only). |
| `vaderSentiment>=3.3.2` | Sentiment NLP | `sentiment_model.py` | Moves to agent context only (not execution path). |
| `yfinance>=0.2.36` | Macro data (DXY, SPX) | `macro_agent.py` | No change (agent context only). |
| `matplotlib>=3.7.0` | Visualisation | Reports, backtesting | No change. |
| `safetensors>=0.4.0` | PyTorch security | RL ensemble | No change. |
| `gymnasium>=1.0.0` | RL training | `core/rl/` | No change (not on intraday path). |

### 1.2 Dependencies That Need Version/Config Changes

| Package | Current | Change Needed | Reason |
|---|---|---|---|
| `ccxt>=4.2.0` | REST + WS exchange client | **Verify ccxt.pro included** in user's install. `ccxt.pro` provides `watch_ohlcv()` and `watch_ticker()` needed by DataEngine. Standard `pip install ccxt` includes ccxt.pro since v4.0. User has `ccxt 4.5.42` installed per CLAUDE.md. ✅ Already sufficient. |
| `PySide6>=6.6.0` | Qt GUI framework | **Becomes optional dependency**. Headless core must run without PySide6. Import guarded: `try: from PySide6... except ImportError: ...`. Still required for GUI mode. |

### 1.3 New Dependencies Required

| Package | Purpose | Design Element | Required By Phase | Size/Impact |
|---|---|---|---|---|
| None | — | — | — | — |

**Key finding: No new Python packages are required.** The intraday redesign uses only libraries already in `requirements.txt`. Specifically:

- **WebSocket (ccxt.pro):** Already included in `ccxt>=4.2.0`. User has 4.5.42.
- **asyncio:** Python stdlib. Already used by ccxt.pro internally. DataEngine will use `asyncio.run()` to drive WS event loop.
- **threading:** Python stdlib. Already used throughout.
- **VWAP calculation:** Implemented in `indicator_library.py` using pandas (no new library).
- **Volume profile:** Implemented using numpy histogram (no new library).
- **Microstructure indicators:** Computed from order book data via existing `ExchangeManager.fetch_order_book()`.

### 1.4 Dependencies to Mark as Optional

| Package | Current Status | New Status | Reason |
|---|---|---|---|
| `PySide6>=6.6.0` | Required | **Optional** (headless mode) | Core engine must run without Qt. GUI is observer only. |
| `pyqtgraph>=0.13.3` | Required | **Optional** (GUI mode only) | Chart rendering. Not needed in headless. |
| `SpeechRecognition>=3.10.0` | Required | **Optional** | Voice input. Not needed for trading. |
| `pyttsx3>=2.90` | Required | **Optional** | Text-to-speech. Not needed for trading. |
| `pyaudio>=0.2.13` | Required | **Optional** | Microphone. Not needed for trading. |
| `torch` (Step 2 install) | Required for FinBERT/RL | **Optional** | FinBERT moves to agent context only. RL not on execution path. |
| `transformers` (Step 3 install) | Required for FinBERT | **Optional** | Same as above. |

---

## 2. Tooling Assessment

### 2.1 Build & Runtime

| Tool | Status | Notes |
|---|---|---|
| Python 3.11.x | ✅ User has 3.11.x on Windows | All code compatible. |
| pip | ✅ Standard | No build system changes needed. |
| CUDA cu124 | ✅ RTX 4070 + cu124 torch | Not on intraday execution path but available. |
| SQLite | ✅ Built into Python | Database engine. No migration tool needed — `_migrate_schema()` handles it. |

### 2.2 Testing

| Tool | Status | Notes |
|---|---|---|
| pytest | ✅ Already used | ~90 test files, 1,652+ passing. Framework stays. |
| unittest.mock | ✅ Python stdlib | Used throughout existing tests. |
| pytest-asyncio | ❌ **Needed** | DataEngine uses `asyncio`. Need `pytest-asyncio` for async test support. **Add to dev dependencies.** |

### 2.3 Development Tools

| Tool | Status | Notes |
|---|---|---|
| Git | ✅ User has git | Repo tracked. |
| YAML config | ✅ `pyyaml` installed | Config management unchanged. |

---

## 3. Platform Constraints

### 3.1 Windows-Specific

| Constraint | Impact | Mitigation |
|---|---|---|
| asyncio event loop on Windows | Windows uses `ProactorEventLoop` by default. ccxt.pro may need `SelectorEventLoop` for some WS protocols. | Set `asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())` at DataEngine startup. Test on Windows specifically. |
| Path separators | `pathlib.Path` used throughout | Already handled correctly. No issues expected. |
| Thread count limits | Windows has higher thread overhead than Linux | Current baseline ~51 threads. Adding DataEngine's asyncio loop + CandleBuilder = ~53-55. Well within limits. |

### 3.2 Network (Singapore VPN → Bybit)

| Constraint | Impact | Mitigation |
|---|---|---|
| 50-150ms RTT | WS message delivery latency | Signal expiry thresholds (6-12s) are 40-200× the RTT. Safe margin. |
| VPN disconnects | WS stream interruption | DataEngine implements 3-miss detection → REST fallback. Auto-reconnect on WS restore. |
| Bybit Demo API | Rate limits differ from production | DataEngine respects `enableRateLimit: true` already set in ExchangeManager. WS has no rate limit on subscribed streams. |

---

## 4. Conclusion

**Dependency impact: MINIMAL.** The intraday redesign introduces zero new runtime dependencies. One dev dependency (`pytest-asyncio`) is needed for testing async code. The architectural changes are purely code-level — new modules, adapted modules, and archived modules — all using existing libraries.

**Highest-risk dependency:** `ccxt.pro` WebSocket stability on Windows with Singapore VPN. This is the only dependency path that changes fundamentally (REST → WS). Mitigation: REST fallback is automatic and uses the exact same code path as the current system.

**PySide6 decoupling:** The most impactful tooling change. Every file importing PySide6 in the `core/` directory must be audit-cleaned. Five files identified in Phase 1B §11.2: `event_bus.py`, `base_agent.py`, `agent_coordinator.py`, `orchestrator_engine.py`, `scanner.py`. Scanner is being replaced entirely; the other 4 need Qt import removal.

---

*End of Phase 1C. Proceed to Phase 1D (Implementation Plan by Module).*
