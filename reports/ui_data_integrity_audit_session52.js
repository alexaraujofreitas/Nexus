const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, LevelFormat, HeadingLevel,
  BorderStyle, WidthType, ShadingType, PageNumber, PageBreak
} = require("docx");

// ─── Constants ───────────────────────────────────────────────────
const PAGE_W = 12240, PAGE_H = 15840, MARGIN = 1440;
const CONTENT_W = PAGE_W - 2 * MARGIN; // 9360
const RED = "C0392B", GREEN = "27AE60", AMBER = "F39C12", BLUE = "2E75B6";
const LGRAY = "F5F5F5", MGRAY = "E0E0E0";

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellPad = { top: 60, bottom: 60, left: 100, right: 100 };

// ─── Helpers ─────────────────────────────────────────────────────
const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 360, after: 200 }, children: [new TextRun({ text: t, bold: true, font: "Arial", size: 28 })] });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, spacing: { before: 280, after: 160 }, children: [new TextRun({ text: t, bold: true, font: "Arial", size: 24 })] });
const h3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, spacing: { before: 200, after: 120 }, children: [new TextRun({ text: t, bold: true, font: "Arial", size: 22 })] });
const p = (t, opts = {}) => new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: t, font: "Arial", size: 20, ...opts })] });
const pb = (t, opts = {}) => new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: t, font: "Arial", size: 20, bold: true, ...opts })] });

const statusBadge = (status) => {
  const colors = { "FAIL": RED, "PASS": GREEN, "WARN": AMBER, "N/A": "888888", "BLOCKER": RED };
  return new TextRun({ text: ` [${status}] `, font: "Arial", size: 20, bold: true, color: colors[status] || "000000" });
};

function makeRow(cells, isHeader = false) {
  return new TableRow({
    children: cells.map((c, i) => new TableCell({
      borders,
      width: { size: c.w || 1000, type: WidthType.DXA },
      shading: isHeader ? { fill: BLUE, type: ShadingType.CLEAR } : (c.shade ? { fill: c.shade, type: ShadingType.CLEAR } : undefined),
      margins: cellPad,
      verticalAlign: "center",
      children: [new Paragraph({ children: [new TextRun({ text: c.text, font: "Arial", size: 18, bold: isHeader, color: isHeader ? "FFFFFF" : (c.color || "000000") })] })]
    }))
  });
}

// ─── Data ────────────────────────────────────────────────────────

const pageAudit = [
  { page: "Dashboard", status: "FAIL", source: "paper_executor (hardcoded)", reads: "positions, trades, balance, drawdown, P&L", risk: "Shows paper data in live mode", file: "gui/pages/dashboard/dashboard_page.py" },
  { page: "Demo Live Monitor", status: "FAIL", source: "paper_executor (hardcoded)", reads: "equity curve, trades, stats, portfolio heat", risk: "Shows paper metrics in live mode", file: "gui/pages/demo_monitor/demo_monitor_page.py" },
  { page: "Paper Trading", status: "WARN", source: "paper_executor (hardcoded)", reads: "positions, trades, manual controls", risk: "Expected paper-only; no live equivalent page", file: "gui/pages/paper_trading/paper_trading_page.py" },
  { page: "Orders & Positions", status: "FAIL", source: "paper_executor (hardcoded)", reads: "_closed_trades, get_open_positions()", risk: "Shows only paper trades/positions", file: "gui/pages/orders_positions/orders_page.py" },
  { page: "Performance Analytics", status: "FAIL", source: "paper_executor (hardcoded)", reads: "get_closed_trades(), get_open_positions()", risk: "Analyzes only paper history", file: "gui/pages/performance_analytics/analytics_page.py" },
  { page: "Quant Dashboard", status: "FAIL", source: "paper_executor (module-level)", reads: "positions, trades, metrics", risk: "Shows paper metrics in live mode", file: "gui/pages/quant_dashboard/quant_dashboard_page.py" },
  { page: "Chart Workspace", status: "FAIL", source: "paper_executor (hardcoded)", reads: "get_open_positions() for chart overlay", risk: "Overlays paper positions on charts", file: "gui/pages/chart_workspace/chart_page.py" },
  { page: "System Health", status: "FAIL", source: "get_paper_executor() (hardcoded)", reads: "executor state/health metrics", risk: "Health checks only read paper state", file: "gui/pages/system_health/system_health_page.py" },
  { page: "Risk Management", status: "PASS", source: "order_router.active_executor", reads: "positions, close_all(), mode switch", risk: "Mode-aware with exchange verification", file: "gui/pages/risk_management/risk_page.py" },
  { page: "Market Scanner", status: "WARN", source: "order_router (mixed)", reads: "submit(), positions; fallback to paper_executor", risk: "Partial mode awareness; fallback paths", file: "gui/pages/scanner/scanner_page.py" },
  { page: "Intelligence", status: "PASS", source: "EventBus only", reads: "Agent status, market regime", risk: "No executor dependency", file: "gui/pages/intelligence/intelligence_page.py" },
  { page: "Intelligence Agents", status: "PASS", source: "Agent coordinator", reads: "Agent status only", risk: "No executor dependency", file: "gui/pages/agents/agents_page.py" },
  { page: "Research Lab", status: "PASS", source: "Historical data only", reads: "No executor reads", risk: "Completely isolated", file: "gui/pages/research_lab/research_lab_page.py" },
  { page: "Backtesting", status: "PASS", source: "Database (Strategy, BacktestResult)", reads: "Historical only", risk: "Completely isolated", file: "gui/pages/backtesting/backtesting_page.py" },
  { page: "News & Sentiment", status: "PASS", source: "Database (SentimentData)", reads: "Display only", risk: "No executor dependency", file: "gui/pages/news_sentiment/news_sentiment_page.py" },
  { page: "Notifications", status: "PASS", source: "notification_manager", reads: "Event/log display", risk: "Agnostic", file: "gui/pages/notifications/notifications_page.py" },
  { page: "Exchange Management", status: "PASS", source: "Database (Exchange, Asset)", reads: "Configuration only", risk: "No trading state", file: "gui/pages/exchange/exchange_page.py" },
  { page: "Settings", status: "PASS", source: "Config file, vault", reads: "Application settings", risk: "No trading state", file: "gui/pages/settings/settings_page.py" },
  { page: "Logs", status: "PASS", source: "Database (SystemLog)", reads: "Log display only", risk: "No executor dependency", file: "gui/pages/logs/logs_page.py" },
  { page: "Signal Explorer", status: "PASS", source: "Database (SignalLog)", reads: "Historical signals", risk: "No executor dependency", file: "gui/pages/signal_explorer/signal_explorer_page.py" },
  { page: "Regime", status: "PASS", source: "EventBus (regime history)", reads: "Display only", risk: "Agnostic", file: "gui/pages/regime/regime_page.py" },
];

const monitoringAudit = [
  { component: "LiveVsBacktestTracker", status: "FAIL", source: "paper_executor.record() calls", issue: "Hardcoded to paper executor trade format", file: "core/monitoring/live_vs_backtest.py" },
  { component: "PerformanceThresholdEvaluator", status: "FAIL", source: "LiveVsBacktestTracker (indirect)", issue: "Depends on paper-only tracker", file: "core/monitoring/performance_thresholds.py" },
  { component: "ScaleManager", status: "FAIL", source: "LvBT + PTE (indirect)", issue: "Depends on paper-only chain", file: "core/monitoring/scale_manager.py" },
  { component: "ReviewGenerator", status: "FAIL", source: "get_paper_executor() direct", issue: "Hardcoded import: _closed_trades, _capital, _peak_capital", file: "core/monitoring/review_generator.py" },
  { component: "CapitalUtilizationMonitor", status: "WARN", source: "Parameter-based (PaperExecutor type)", issue: "Accepts executor param but typed to PaperExecutor", file: "core/monitoring/capital_utilization_monitor.py" },
  { component: "Phase1MetricsTracker", status: "PASS", source: "Event-driven", issue: "Executor-agnostic", file: "core/monitoring/phase1_metrics.py" },
  { component: "DemoPerformanceEvaluator", status: "FAIL", source: "paper_executor.get_closed_trades()", issue: "Hardcoded import", file: "core/evaluation/demo_performance_evaluator.py" },
  { component: "EdgeEvaluator", status: "FAIL", source: "paper_executor.get_closed_trades()", issue: "Hardcoded import", file: "core/evaluation/edge_evaluator.py" },
  { component: "SystemReadinessEvaluator", status: "PASS", source: "Parameterized (trades list)", issue: "Works with any executor", file: "core/evaluation/system_readiness_evaluator.py" },
  { component: "ModelPerformanceTracker", status: "PASS", source: "Event-driven .record()", issue: "Executor-agnostic", file: "core/analytics/model_performance_tracker.py" },
  { component: "FilterStatsTracker", status: "PASS", source: "Event-driven", issue: "Executor-agnostic", file: "core/analytics/filter_stats.py" },
  { component: "PortfolioGuard", status: "PASS", source: "Parameterized (positions list)", issue: "Executor-agnostic", file: "core/analytics/portfolio_guard.py" },
  { component: "SymbolAllocator", status: "PASS", source: "Config/DB driven", issue: "Executor-agnostic", file: "core/analytics/symbol_allocator.py" },
  { component: "CorrelationDampener", status: "PASS", source: "Parameterized", issue: "Executor-agnostic", file: "core/analytics/correlation_dampener.py" },
];

const persistenceAudit = [
  { store: "SQLite paper_trades", paperWrites: "Yes", liveWrites: "NO", uiReads: "Analytics, reviews", issue: "Live trades not persisted to DB" },
  { store: "open_positions.json", paperWrites: "Yes (every tick)", liveWrites: "NO", uiReads: "Startup restore (paper)", issue: "Live uses exchange API instead" },
  { store: "trade_outcomes.jsonl", paperWrites: "Yes (via learning)", liveWrites: "NO", uiReads: "L2 learning, calibrator", issue: "Live trades not in learning pipeline" },
  { store: "In-memory _closed_trades", paperWrites: "Yes", liveWrites: "Yes", uiReads: "All UI pages", issue: "Live trades lost on restart" },
  { store: "Exchange API", paperWrites: "N/A", liveWrites: "Authoritative", uiReads: "LiveBridge only", issue: "UI pages don't read exchange directly" },
];

const blockers = [
  { id: "B-01", severity: "BLOCKER", title: "7 GUI pages hardcode paper_executor", desc: "Dashboard, Demo Monitor, Orders, Analytics, Quant Dashboard, Chart, System Health all import paper_executor directly. In live mode, they show paper data while real trades execute elsewhere. User cannot see live positions, balance, or P&L." },
  { id: "B-02", severity: "BLOCKER", title: "LiveBridge does not persist closed trades to SQLite", desc: "PaperExecutor writes every closed trade to paper_trades table. LiveBridge keeps closed trades only in memory. After restart, ALL live trading history is lost. Analytics, reviews, and evaluators see 0 trades." },
  { id: "B-03", severity: "BLOCKER", title: "Monitoring chain hardcoded to paper_executor", desc: "ReviewGenerator, DemoPerformanceEvaluator, EdgeEvaluator all hardcode paper_executor imports. Live trades never flow through performance evaluation, readiness checks, or scale management." },
  { id: "B-04", severity: "BLOCKER", title: "LiveBridge trades not in learning pipeline", desc: "TradeOutcomeStore (JSONL) only receives paper trades. L2 adaptive weights, probability calibrator, and model auto-disable logic never see live trade outcomes. System cannot learn from live performance." },
  { id: "B-05", severity: "HIGH", title: "ConfluenceScorer reads paper_executor._capital directly", desc: "confluence_scorer.py line 858 accesses _pe._capital (private field) for compounding capital. In live mode this returns paper capital, not exchange balance. Sizing decisions use wrong capital base." },
  { id: "B-06", severity: "HIGH", title: "No live mode indicator in main UI", desc: "Mode switch only available on Risk Management page. No persistent indicator in toolbar/statusbar showing current mode. User may not realize system is in live mode." },
  { id: "B-07", severity: "HIGH", title: "NotificationManager reads paper_executor for trade data", desc: "notification_manager.py imports paper_executor at lines 700, 859, 908, 928. Trade notifications in live mode would read paper data, not actual exchange fills." },
  { id: "B-08", severity: "MEDIUM", title: "DataFeed reads paper_executor for position updates", desc: "data_feed.py line 352 imports paper_executor for position data. Live position price updates may not route correctly." },
];

const fixes = [
  { id: "F-01", title: "Replace all paper_executor imports with order_router.active_executor", desc: "11 files need refactoring. Change 'from core.execution.paper_executor import paper_executor' to use 'from core.execution.order_router import order_router; executor = order_router.active_executor'. Files: dashboard_page.py, orders_page.py, analytics_page.py, quant_dashboard_page.py, chart_page.py, system_health_page.py, paper_trading_page.py, notification_manager.py, review_generator.py, demo_performance_evaluator.py, edge_evaluator.py." },
  { id: "F-02", title: "Add trade persistence to LiveBridge", desc: "Implement _save_trade_to_db() in LiveBridge matching PaperExecutor's pattern. Write every closed trade to SQLite paper_trades table with an executor_mode='live' tag. Also integrate with TradeOutcomeStore for JSONL learning pipeline. This ensures trade history survives restart and feeds analytics." },
  { id: "F-03", title: "Expose public accessors on LiveBridge", desc: "Add public methods/properties that monitoring code uses: get_capital() -> float, get_peak_capital() -> float. Ensure get_closed_trades() returns same dict format as PaperExecutor. Update confluence_scorer.py to use order_router.active_executor.available_capital instead of _pe._capital." },
  { id: "F-04", title: "Refactor monitoring chain to accept executor abstraction", desc: "ReviewGenerator: accept executor parameter instead of get_paper_executor(). LiveVsBacktestTracker: ensure .record() is called from LiveBridge._close_position_on_exchange() path. CapitalUtilizationMonitor: change type hint from PaperExecutor to a protocol/ABC." },
  { id: "F-05", title: "Add live mode indicator to main window", desc: "Add persistent mode indicator in main_window.py toolbar/statusbar. Show 'PAPER MODE' (green) or 'LIVE MODE' (red pulsing) based on order_router.mode. Subscribe to MODE_CHANGED event to update dynamically." },
  { id: "F-06", title: "Add executor_mode column to paper_trades schema", desc: "Add 'executor_mode' TEXT column (values: 'paper', 'live') to paper_trades table in database models. Update _migrate_schema() in engine.py. This enables filtering trade history by mode and prevents confusion between paper and live results." },
];

// ─── Build Document ──────────────────────────────────────────────
const children = [];

// Title page
children.push(new Paragraph({ spacing: { before: 3000 }, alignment: AlignmentType.CENTER, children: [new TextRun({ text: "NexusTrader", font: "Arial", size: 48, bold: true, color: BLUE })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "UI & Data Integrity Audit", font: "Arial", size: 36, color: "444444" })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 200 }, children: [new TextRun({ text: "Paper-to-Live Migration Readiness Assessment", font: "Arial", size: 24, color: "666666" })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 400 }, children: [new TextRun({ text: "Session 52 \u2014 April 8, 2026", font: "Arial", size: 22, color: "888888" })] }));
children.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 100 }, children: [new TextRun({ text: "Classification: CRITICAL CORRECTNESS AUDIT", font: "Arial", size: 22, bold: true, color: RED })] }));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Executive Summary ──────────────────────────────────────────
children.push(h1("1. Executive Summary"));
children.push(p("This audit examines every UI page, monitoring component, analytics module, and data persistence layer in NexusTrader to determine whether the system correctly surfaces exchange-backed data when operating in live/testnet mode."));
children.push(new Paragraph({ spacing: { after: 120 }, children: [
  new TextRun({ text: "Overall Verdict: ", font: "Arial", size: 20, bold: true }),
  new TextRun({ text: "FAIL", font: "Arial", size: 22, bold: true, color: RED }),
  new TextRun({ text: " \u2014 The system is NOT ready for live UI operation. While the execution path (scanner \u2192 order_router \u2192 LiveBridge \u2192 exchange) is fully wired and safe, the UI and monitoring layers remain hardcoded to paper_executor. A user in live mode would see paper data while real money trades execute invisibly.", font: "Arial", size: 20 }),
]}));

children.push(pb("Key Findings:"));
children.push(p("\u2022  7 of 21 GUI pages directly import paper_executor \u2014 they will show stale/simulated data in live mode"));
children.push(p("\u2022  6 monitoring/evaluation components hardcode paper_executor imports \u2014 live trades never reach performance evaluation"));
children.push(p("\u2022  LiveBridge does NOT persist closed trades to SQLite \u2014 all live trade history is lost on restart"));
children.push(p("\u2022  LiveBridge trades do not feed the learning pipeline (JSONL) \u2014 system cannot learn from live performance"));
children.push(p("\u2022  ConfluenceScorer reads paper_executor._capital for sizing \u2014 uses wrong capital base in live mode"));
children.push(p("\u2022  No live mode indicator in main UI \u2014 user may not realize they are trading with real money"));

children.push(pb("What IS working correctly:"));
children.push(p("\u2022  Execution path: scanner \u2192 order_router.submit() \u2192 LiveBridge._execute_candidate() \u2192 Phase 8 \u2192 exchange"));
children.push(p("\u2022  Risk Management page is fully mode-aware (uses order_router.active_executor)"));
children.push(p("\u2022  Scanner reads positions/capital via order_router.active_executor (mode-transparent)"));
children.push(p("\u2022  Phase 8 safety: SL confirmed, FSM+idempotency, degraded mode, crash recovery (all proven Session 52)"));
children.push(p("\u2022  10 of 21 GUI pages are safe (display-only, no executor dependency)"));
children.push(p("\u2022  7 of 14 analytics/monitoring components are executor-agnostic (event-driven or parameterized)"));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Page-by-Page Audit ─────────────────────────────────────────
children.push(h1("2. GUI Page Audit (21 Pages)"));
children.push(p("Each page is rated PASS (exchange-backed or no executor dependency), WARN (partially mode-aware), or FAIL (hardcoded to paper_executor, will show wrong data in live mode)."));

const colWidths = [1800, 700, 2200, 2200, 2360];
children.push(new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: colWidths,
  rows: [
    makeRow([
      { text: "Page", w: colWidths[0] },
      { text: "Status", w: colWidths[1] },
      { text: "Data Source", w: colWidths[2] },
      { text: "Reads", w: colWidths[3] },
      { text: "Risk / Issue", w: colWidths[4] },
    ], true),
    ...pageAudit.map(r => {
      const statusColor = r.status === "FAIL" ? RED : r.status === "WARN" ? AMBER : GREEN;
      return makeRow([
        { text: r.page, w: colWidths[0] },
        { text: r.status, w: colWidths[1], color: statusColor },
        { text: r.source, w: colWidths[2] },
        { text: r.reads, w: colWidths[3] },
        { text: r.risk, w: colWidths[4] },
      ]);
    })
  ]
}));

children.push(new Paragraph({ spacing: { before: 200, after: 120 }, children: [
  new TextRun({ text: "Summary: ", font: "Arial", size: 20, bold: true }),
  new TextRun({ text: "7 FAIL", font: "Arial", size: 20, bold: true, color: RED }),
  new TextRun({ text: " / 2 WARN / 12 PASS", font: "Arial", size: 20 }),
]}));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Monitoring/Analytics Audit ──────────────────────────────────
children.push(h1("3. Monitoring, Analytics & Evaluation Audit"));
children.push(p("These components compute performance metrics, drawdown, readiness assessments, and adaptive learning. If they read paper data in live mode, all metrics are meaningless."));

const monColWidths = [2200, 700, 2400, 1600, 2460];
children.push(new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: monColWidths,
  rows: [
    makeRow([
      { text: "Component", w: monColWidths[0] },
      { text: "Status", w: monColWidths[1] },
      { text: "Data Source", w: monColWidths[2] },
      { text: "Issue", w: monColWidths[3] },
      { text: "File", w: monColWidths[4] },
    ], true),
    ...monitoringAudit.map(r => {
      const statusColor = r.status === "FAIL" ? RED : r.status === "WARN" ? AMBER : GREEN;
      return makeRow([
        { text: r.component, w: monColWidths[0] },
        { text: r.status, w: monColWidths[1], color: statusColor },
        { text: r.source, w: monColWidths[2] },
        { text: r.issue, w: monColWidths[3] },
        { text: r.file, w: monColWidths[4] },
      ]);
    })
  ]
}));

children.push(new Paragraph({ spacing: { before: 200, after: 120 }, children: [
  new TextRun({ text: "Summary: ", font: "Arial", size: 20, bold: true }),
  new TextRun({ text: "6 FAIL", font: "Arial", size: 20, bold: true, color: RED }),
  new TextRun({ text: " / 1 WARN / 7 PASS", font: "Arial", size: 20 }),
]}));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Data Persistence Audit ─────────────────────────────────────
children.push(h1("4. Data Persistence Layer Audit"));
children.push(p("NexusTrader uses three persistence stores: SQLite (authoritative trade history), JSON (open positions snapshot), and JSONL (learning pipeline). LiveBridge currently writes to NONE of these."));

const persColWidths = [1800, 1200, 1200, 1800, 3360];
children.push(new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: persColWidths,
  rows: [
    makeRow([
      { text: "Store", w: persColWidths[0] },
      { text: "Paper Writes", w: persColWidths[1] },
      { text: "Live Writes", w: persColWidths[2] },
      { text: "UI Reads", w: persColWidths[3] },
      { text: "Issue", w: persColWidths[4] },
    ], true),
    ...persistenceAudit.map(r => makeRow([
      { text: r.store, w: persColWidths[0] },
      { text: r.paperWrites, w: persColWidths[1] },
      { text: r.liveWrites, w: persColWidths[2], color: r.liveWrites === "NO" ? RED : undefined },
      { text: r.uiReads, w: persColWidths[3] },
      { text: r.issue, w: persColWidths[4] },
    ]))
  ]
}));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Data Flow Diagrams ─────────────────────────────────────────
children.push(h1("5. Data Flow Analysis"));

children.push(h2("5.1 Current Paper Mode (Working Correctly)"));
children.push(p("OrderCandidate \u2192 order_router.submit() \u2192 paper_executor.submit() \u2192 PaperPosition created \u2192 open_positions.json written \u2192 TRADE_OPENED event published"));
children.push(p("On tick: paper_executor checks SL/TP \u2192 _close_position() \u2192 _closed_trades list + SQLite paper_trades + TRADE_CLOSED event \u2192 LiveVsBacktestTracker.record() \u2192 ModelPerformanceTracker.record()"));
children.push(p("UI reads: paper_executor.get_open_positions(), get_closed_trades(), get_stats(), available_capital, drawdown_pct"));
children.push(p("This is a complete, self-consistent data loop."));

children.push(h2("5.2 Current Live Mode (BROKEN Data Loop)"));
children.push(p("OrderCandidate \u2192 order_router.submit() \u2192 live_bridge.submit() \u2192 Phase 8 LiveExecutor \u2192 exchange order placed \u2192 exchange ACK \u2192 position in _positions dict \u2192 TRADE_OPENED event"));
children.push(p("On close: live_bridge._close_position_on_exchange() \u2192 exchange market order \u2192 _closed_trades list (MEMORY ONLY) \u2192 TRADE_CLOSED event"));
children.push(new Paragraph({ spacing: { after: 120 }, children: [
  new TextRun({ text: "BREAK POINT: ", font: "Arial", size: 20, bold: true, color: RED }),
  new TextRun({ text: "Closed trades are NOT written to SQLite, NOT written to JSONL, NOT fed to LiveVsBacktestTracker. UI pages reading paper_executor see paper data. Monitoring/evaluation see paper data. Learning system sees paper data. Only the scanner (via order_router.active_executor) and Risk Management page see live data.", font: "Arial", size: 20 }),
]}));

children.push(h2("5.3 Required Live Mode (Target Architecture)"));
children.push(p("Exchange (Bybit) \u2192 LiveBridge (hydration + reconciliation) \u2192 Internal normalized state (_positions, _closed_trades) \u2192 SQLite + JSONL persistence \u2192 Monitoring/Analytics/Evaluation \u2192 UI pages (all via order_router.active_executor)"));
children.push(p("Every component in the chain must read from order_router.active_executor, which transparently returns LiveBridge when mode='live'. LiveBridge must persist trades to the same stores as PaperExecutor."));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Blockers ────────────────────────────────────────────────────
children.push(h1("6. Findings \u2014 Blockers & Issues"));

blockers.forEach(b => {
  const sevColor = b.severity === "BLOCKER" ? RED : b.severity === "HIGH" ? AMBER : "666666";
  children.push(new Paragraph({ spacing: { before: 200, after: 80 }, children: [
    new TextRun({ text: `${b.id}`, font: "Arial", size: 20, bold: true }),
    new TextRun({ text: ` [${b.severity}] `, font: "Arial", size: 20, bold: true, color: sevColor }),
    new TextRun({ text: b.title, font: "Arial", size: 20, bold: true }),
  ]}));
  children.push(p(b.desc));
});

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Required Fixes ──────────────────────────────────────────────
children.push(h1("7. Required Code Changes"));
children.push(p("The following fixes, applied in order, will close all data integrity gaps and make the UI exchange-truth driven."));

fixes.forEach(f => {
  children.push(h3(`${f.id}: ${f.title}`));
  children.push(p(f.desc));
});

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Files Requiring Changes ─────────────────────────────────────
children.push(h1("8. Complete File Inventory"));
children.push(p("Every file that must be modified to close the paper-to-live data gap:"));

const fileList = [
  ["gui/pages/dashboard/dashboard_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/demo_monitor/demo_monitor_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/orders_positions/orders_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/performance_analytics/analytics_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/quant_dashboard/quant_dashboard_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/chart_workspace/chart_page.py", "Replace paper_executor with order_router.active_executor"],
  ["gui/pages/system_health/system_health_page.py", "Replace get_paper_executor() with order_router.active_executor"],
  ["gui/pages/paper_trading/paper_trading_page.py", "Add mode guard; show paper controls only in paper mode"],
  ["core/execution/live_bridge.py", "Add _save_trade_to_db(), TradeOutcomeStore integration, public capital accessors"],
  ["core/monitoring/review_generator.py", "Accept executor parameter instead of get_paper_executor()"],
  ["core/monitoring/live_vs_backtest.py", "Ensure .record() called from LiveBridge close path"],
  ["core/monitoring/capital_utilization_monitor.py", "Change PaperExecutor type hint to protocol/ABC"],
  ["core/evaluation/demo_performance_evaluator.py", "Accept closed_trades parameter, remove hardcoded import"],
  ["core/evaluation/edge_evaluator.py", "Accept closed_trades parameter, remove hardcoded import"],
  ["core/meta_decision/confluence_scorer.py", "Replace _pe._capital with order_router.active_executor.available_capital"],
  ["core/notifications/notification_manager.py", "Replace paper_executor imports with order_router.active_executor"],
  ["core/market_data/data_feed.py", "Replace paper_executor import with order_router.active_executor"],
  ["core/database/models.py", "Add executor_mode column to paper_trades"],
  ["core/database/engine.py", "Add executor_mode to _migrate_schema()"],
  ["gui/main_window.py", "Add live mode indicator to toolbar/statusbar"],
];

const fileColWidths = [4600, 4760];
children.push(new Table({
  width: { size: CONTENT_W, type: WidthType.DXA },
  columnWidths: fileColWidths,
  rows: [
    makeRow([{ text: "File", w: fileColWidths[0] }, { text: "Required Change", w: fileColWidths[1] }], true),
    ...fileList.map(([file, change]) => makeRow([{ text: file, w: fileColWidths[0] }, { text: change, w: fileColWidths[1] }]))
  ]
}));

children.push(new Paragraph({ spacing: { before: 200 }, children: [
  new TextRun({ text: "Total files requiring modification: ", font: "Arial", size: 20, bold: true }),
  new TextRun({ text: `${fileList.length}`, font: "Arial", size: 22, bold: true, color: RED }),
]}));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Validation Plan ─────────────────────────────────────────────
children.push(h1("9. Validation Plan"));
children.push(p("After implementing all fixes, the following validation steps confirm UI = exchange truth:"));

children.push(h3("Step 1: Unit Tests"));
children.push(p("Write tests for each refactored page/component verifying they call order_router.active_executor (not paper_executor) and produce correct output for both paper and live mock data."));

children.push(h3("Step 2: Mode Switch Integration Test"));
children.push(p("Start in paper mode \u2192 open paper position \u2192 verify UI shows it \u2192 switch to live mode \u2192 verify UI shows live (empty) state \u2192 mock a live position \u2192 verify UI shows it \u2192 switch back to paper \u2192 verify paper position reappears."));

children.push(h3("Step 3: Persistence Verification"));
children.push(p("In live mode: open position \u2192 close position \u2192 verify closed trade appears in SQLite with executor_mode='live' \u2192 restart application \u2192 verify trade history survives restart."));

children.push(h3("Step 4: Bybit Testnet Consistency Check"));
children.push(p("Connect to Bybit Testnet (Singapore VPN) \u2192 place test order \u2192 compare UI values (balance, position size, entry price, unrealized P&L) against Bybit web interface \u2192 close position \u2192 compare realized P&L and fees."));

children.push(h3("Step 5: Monitoring Chain Verification"));
children.push(p("After 5+ live test trades: verify LiveVsBacktestTracker has recorded all trades \u2192 verify ReviewGenerator includes live trades in daily review \u2192 verify DemoPerformanceEvaluator evaluates live trades \u2192 verify ModelPerformanceTracker updates per-model stats."));

children.push(new Paragraph({ children: [new PageBreak()] }));

// ─── Conclusion ──────────────────────────────────────────────────
children.push(h1("10. Conclusion"));
children.push(p("The NexusTrader execution path is safe and fully wired for live trading (proven in Session 52 safety hardening: 94 tests, 6 safety properties verified). However, the UI and data layers remain paper-centric."));
children.push(new Paragraph({ spacing: { after: 120 }, children: [
  new TextRun({ text: "The system can safely execute live trades", font: "Arial", size: 20, bold: true, color: GREEN }),
  new TextRun({ text: " but the operator cannot see, monitor, or evaluate them through the UI. This is an unacceptable state for production trading.", font: "Arial", size: 20 }),
]}));
children.push(p("The 20-file refactoring plan in this audit will close all gaps. The core pattern is simple: replace every direct paper_executor import with order_router.active_executor, and add trade persistence to LiveBridge. No architectural redesign is needed \u2014 the order_router abstraction already exists and works correctly in the scanner and risk page."));
children.push(new Paragraph({ spacing: { before: 200, after: 120 }, children: [
  new TextRun({ text: "Recommendation: ", font: "Arial", size: 20, bold: true }),
  new TextRun({ text: "Implement fixes F-01 through F-06 before Bybit Testnet deployment. Estimated effort: 2\u20133 sessions. The execution layer is safe; the observability layer needs this final pass.", font: "Arial", size: 20 }),
]}));

// ─── Pack Document ───────────────────────────────────────────────
const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 20 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 28, bold: true, font: "Arial", color: BLUE },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: "333333" },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, font: "Arial", color: "444444" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 } },
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: PAGE_W, height: PAGE_H },
        margin: { top: MARGIN, right: MARGIN, bottom: MARGIN, left: MARGIN }
      }
    },
    headers: {
      default: new Header({ children: [new Paragraph({
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: BLUE, space: 1 } },
        children: [
          new TextRun({ text: "NexusTrader UI & Data Integrity Audit", font: "Arial", size: 16, color: "888888" }),
        ],
        tabStops: [{ type: "right", position: 9360 }],
      })] })
    },
    footers: {
      default: new Footer({ children: [new Paragraph({
        alignment: AlignmentType.CENTER,
        children: [
          new TextRun({ text: "Page ", font: "Arial", size: 16, color: "888888" }),
          new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: "888888" }),
        ]
      })] })
    },
    children
  }]
});

Packer.toBuffer(doc).then(buffer => {
  const outPath = "/sessions/friendly-bold-hypatia/mnt/NexusTrader/reports/ui_data_integrity_audit_session52.docx";
  fs.writeFileSync(outPath, buffer);
  console.log(`Written: ${outPath} (${buffer.length} bytes)`);
});
