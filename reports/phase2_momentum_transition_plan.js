const fs = require("fs");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
  ShadingType, PageNumber, PageBreak, LevelFormat, TabStopType, TabStopPosition
} = require("docx");

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const noBorder = { style: BorderStyle.NONE, size: 0 };
const noBorders = { top: noBorder, bottom: noBorder, left: noBorder, right: noBorder };
const cellPad = { top: 80, bottom: 80, left: 120, right: 120 };
const W = 9360; // content width (US Letter, 1" margins)

function txt(t, opts = {}) { return new TextRun({ text: t, font: "Arial", ...opts }); }
function bold(t, opts = {}) { return txt(t, { bold: true, ...opts }); }
function para(children, opts = {}) { return new Paragraph({ children: Array.isArray(children) ? children : [children], ...opts }); }
function heading(t, level) { return new Paragraph({ heading: level, children: [txt(t, { bold: true })] }); }
function spacer() { return para([txt("")]); }

function cell(children, w, opts = {}) {
  return new TableCell({
    borders, width: { size: w, type: WidthType.DXA }, margins: cellPad,
    children: Array.isArray(children) ? children : [children], ...opts
  });
}
function hdrCell(children, w, opts = {}) {
  return cell(children, w, { shading: { fill: "1B3A5C", type: ShadingType.CLEAR }, ...opts });
}
function hdrTxt(t) { return para([txt(t, { bold: true, color: "FFFFFF", size: 20 })]); }

function statusBadge(status) {
  const colors = { "ACTIVE": "2E7D32", "DISABLED": "C62828", "RESEARCH": "E65100", "NEW": "1565C0", "GATED": "6A1B9A" };
  return txt(` [${status}]`, { bold: true, color: colors[status] || "333333", size: 18 });
}

// ════════════════════════════════════════════════════════════
// DOCUMENT
// ════════════════════════════════════════════════════════════

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, font: "Arial", color: "1B3A5C" },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: "2E5984" },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 24, bold: true, font: "Arial", color: "3A7AB5" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 } },
    ]
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [
        { level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
        { level: 1, format: LevelFormat.BULLET, text: "\u2013", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 1080, hanging: 360 } } } },
      ]},
      { reference: "numbers", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ]},
      { reference: "numbers2", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ]},
      { reference: "numbers3", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ]},
      { reference: "numbers4", levels: [
        { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } } },
      ]},
    ]
  },
  sections: [
    // ── COVER PAGE ──
    {
      properties: {
        page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } }
      },
      children: [
        spacer(), spacer(), spacer(), spacer(), spacer(),
        para([txt("NexusTrader", { bold: true, size: 56, color: "1B3A5C" })], { alignment: AlignmentType.CENTER }),
        spacer(),
        para([txt("Phase 2 Strategy Development Plan", { size: 36, color: "2E5984" })], { alignment: AlignmentType.CENTER }),
        para([txt("Momentum / Transition / Breakout Systems", { size: 28, color: "5A5A5A", italics: true })], { alignment: AlignmentType.CENTER }),
        spacer(), spacer(),
        para([txt("Replaces: RAM (Mean-Reversion) \u2014 FAILED", { size: 22, color: "C62828" })], { alignment: AlignmentType.CENTER }),
        spacer(),
        new Paragraph({
          alignment: AlignmentType.CENTER,
          border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: "1B3A5C", space: 1 } },
          children: [txt("")]
        }),
        spacer(),
        para([txt("Session 51  |  March 30, 2026  |  v2.0", { size: 22, color: "777777" })], { alignment: AlignmentType.CENTER }),
        spacer(), spacer(), spacer(), spacer(), spacer(), spacer(),
        // Status box
        new Table({
          width: { size: 6000, type: WidthType.DXA },
          columnWidths: [6000],
          rows: [new TableRow({ children: [
            new TableCell({
              borders: { top: { style: BorderStyle.SINGLE, size: 2, color: "1B3A5C" },
                         bottom: { style: BorderStyle.SINGLE, size: 2, color: "1B3A5C" },
                         left: { style: BorderStyle.SINGLE, size: 6, color: "1B3A5C" },
                         right: { style: BorderStyle.SINGLE, size: 2, color: "1B3A5C" } },
              width: { size: 6000, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 200, right: 200 },
              children: [
                para([bold("STATUS: "), txt("Awaiting Approval", { color: "E65100" })]),
                para([bold("SCOPE: "), txt("3 models + infrastructure activation")]),
                para([bold("BASELINE: "), txt("PBL+SLC PF=1.2758 (n=1,731 pre-opt / 1,412 post-opt)")]),
                para([bold("CONSTRAINT: "), txt("No mean-reversion. Momentum & transition only.")]),
              ]
            })
          ]})]
        }),
      ]
    },

    // ── MAIN CONTENT ──
    {
      properties: {
        page: { size: { width: 12240, height: 15840 }, margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 } }
      },
      headers: {
        default: new Header({ children: [
          new Paragraph({
            border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: "1B3A5C", space: 1 } },
            tabStops: [{ type: TabStopType.RIGHT, position: TabStopPosition.MAX }],
            children: [
              txt("NexusTrader Phase 2 Plan", { size: 18, color: "999999" }),
              txt("\tMomentum / Transition / Breakout", { size: 18, color: "999999" }),
            ]
          })
        ]})
      },
      footers: {
        default: new Footer({ children: [
          new Paragraph({
            alignment: AlignmentType.CENTER,
            children: [txt("Page ", { size: 18, color: "999999" }), new TextRun({ children: [PageNumber.CURRENT], size: 18, color: "999999" })]
          })
        ]})
      },
      children: [
        // ═══════════════════════════════════════════
        // 1. EXECUTIVE SUMMARY
        // ═══════════════════════════════════════════
        heading("1. Executive Summary", HeadingLevel.HEADING_1),
        para([
          txt("Phase 2 (Strategy Development) originally targeted mean-reversion via the RangeAccumulationModel (RAM). After exhaustive validation across 16 parameter configurations, RAM "),
          bold("failed structurally"),
          txt(" \u2014 no configuration achieved PF \u2265 1.18 at production fees. Root cause: 30-minute crypto is fundamentally momentum-dominated; mean-reversion edges are transient and fee-destroyed at this timeframe."),
        ]),
        spacer(),
        para([txt("This plan replaces the failed Phase 2 with a ", { size: 22 }), bold("momentum/transition-focused system", { size: 22 }), txt(" comprising three integrated models and targeted infrastructure activation:", { size: 22 })]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [500, 2200, 6660],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("#")], 500), hdrCell([hdrTxt("Component")], 2200), hdrCell([hdrTxt("Description")], 6660)
            ]}),
            new TableRow({ children: [
              cell([para([bold("1")])], 500),
              cell([para([bold("TransitionExecutionModel")])], 2200),
              cell([para([txt("Trades validated regime transitions (ranging\u2192trending). Uses Phase 1 TransitionDetector infrastructure. Primary: transition-based early entries.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("2")])], 500),
              cell([para([bold("MomentumBreakout v2")])], 2200),
              cell([para([txt("Refined MB restricted to volatility_expansion regime + SOL/ETH only (BTC excluded per Session 49 evidence). Confirms and scales into positions detected by TransitionExecutionModel.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("3")])], 500),
              cell([para([bold("DonchianBreakout v2")])], 2200),
              cell([para([txt("Session 48 research candidate with tightened parameters: lookback 40+, vol_mult 1.8+, 4h HTF gate, restricted ACTIVE_REGIMES. Clean channel breakout system.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("4")])], 500),
              cell([para([bold("Infrastructure")])], 2200),
              cell([para([txt("Activate RegimeCapitalAllocator (regime-scaled sizing) and CoverageGuarantee (graduated fallback). Coordinate all three models via Research-Priority orchestration.")])], 6660),
            ]}),
          ]
        }),
        spacer(),
        para([bold("Acceptance criteria (per model): "), txt("PF \u2265 1.18 with fees (0.04%/side), MaxDD \u2264 25%, n \u2265 200, combined portfolio PF \u2265 baseline (1.2758).")]),
        para([bold("Hard constraint: "), txt("No mean-reversion models. All components must exploit momentum or transition edges.")]),

        // ═══════════════════════════════════════════
        // 2. WHY RAM FAILED
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("2. Why RAM Failed (Lessons Learned)", HeadingLevel.HEADING_1),

        para([txt("The RAM failure provides critical constraints for the new Phase 2:")]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2500, 6860],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Failure Mode")], 2500), hdrCell([hdrTxt("Lesson for New Phase 2")], 6860)
            ]}),
            new TableRow({ children: [
              cell([para([bold("Structural: mean-reversion at 30m")])], 2500),
              cell([para([txt("30m crypto ranges are transitional, not persistent. Ranges break into trends \u2014 we should trade the breakout, not the reversion.")])], 6860),
            ]}),
            new TableRow({ children: [
              cell([para([bold("Fee destruction")])], 2500),
              cell([para([txt("Thin-edge strategies with high trade counts (4,642 trades) are destroyed by 0.04%/side fees. New models must have wide R:R (3:1+) or high WR (55%+).")])], 6860),
            ]}),
            new TableRow({ children: [
              cell([para([bold("Permissive params")])], 2500),
              cell([para([txt("Default params too loose = indiscriminate entries. All new models start with strict params and relax only if backtests justify it.")])], 6860),
            ]}),
            new TableRow({ children: [
              cell([para([bold("No regime restriction")])], 2500),
              cell([para([txt("ACTIVE_REGIMES=[] fired in all regimes. New models must have explicit regime gates matching their edge thesis.")])], 6860),
            ]}),
          ]
        }),

        // ═══════════════════════════════════════════
        // 3. CURRENT SYSTEM BASELINE
        // ═══════════════════════════════════════════
        spacer(),
        heading("3. Current System Baseline", HeadingLevel.HEADING_1),
        para([txt("All new models must improve upon or at minimum not degrade this baseline:")]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2000, 1200, 1200, 1200, 1200, 1280, 1280],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Model")], 2000), hdrCell([hdrTxt("Status")], 1200),
              hdrCell([hdrTxt("WR")], 1200), hdrCell([hdrTxt("PF(0-fee)")], 1200),
              hdrCell([hdrTxt("PF(fees)")], 1200), hdrCell([hdrTxt("n")], 1280), hdrCell([hdrTxt("Regime")], 1280),
            ]}),
            new TableRow({ children: [
              cell([para([bold("PullbackLong")])], 2000), cell([para([txt("Active", { color: "2E7D32" })])], 1200),
              cell([para([txt("44.6%")])], 1200), cell([para([txt("1.185")])], 1200),
              cell([para([txt("\u2014")])], 1200), cell([para([txt("283")])], 1280), cell([para([txt("bull_trend")])], 1280),
            ]}),
            new TableRow({ children: [
              cell([para([bold("SwingLowCont.")])], 2000), cell([para([txt("Active", { color: "2E7D32" })])], 1200),
              cell([para([txt("60.9%")])], 1200), cell([para([txt("1.5455")])], 1200),
              cell([para([txt("\u2014")])], 1200), cell([para([txt("1,129")])], 1280), cell([para([txt("bear_trend")])], 1280),
            ]}),
            new TableRow({ children: [
              cell([para([bold("Combined")])], 2000, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("Baseline")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("56.4%")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1.5462")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1.441")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1,412")])], 1280, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("\u2014")])], 1280, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
            ]}),
          ]
        }),
        spacer(),
        para([txt("Session 50 optimized parameters: sl=3.0\u00D7ATR, tp=4.0\u00D7ATR, ema_prox=0.4, rsi_min=45, wick_strength=1.5. Parity test baselines updated to n=1,412 / PF=1.5462 / PF(fees)=1.441.")]),

        // ═══════════════════════════════════════════
        // 4. MODEL 1: TransitionExecutionModel
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("4. Model 1: TransitionExecutionModel (TEM)", HeadingLevel.HEADING_1),

        heading("4.1 Thesis", HeadingLevel.HEADING_2),
        para([txt("Regime transitions are the highest-edge moments in crypto markets. When price shifts from ranging to trending (or from compression to expansion), directional momentum is strongest and most predictable. The TransitionExecutionModel captures this edge by entering at the moment of validated transition, before the trend is fully established.")]),

        heading("4.2 Architecture", HeadingLevel.HEADING_2),
        para([bold("Signal source: "), txt("TransitionDetector (Phase 1 infrastructure, already implemented)")]),
        para([bold("Transition types targeted:")]),

        para([bold("TRANSITION_BREAKOUT"), txt(" \u2014 ranging \u2192 bull_trend or bear_trend")], { numbering: { reference: "bullets", level: 0 } }),
        para([bold("TRANSITION_EXPANSION"), txt(" \u2014 vol_compression / squeeze \u2192 vol_expansion")], { numbering: { reference: "bullets", level: 0 } }),
        para([bold("TRANSITION_TREND_FORMING"), txt(" \u2014 uncertain / accumulation \u2192 bull_trend")], { numbering: { reference: "bullets", level: 0 } }),
        spacer(),

        para([bold("Entry logic:")]),
        para([txt("TransitionDetector fires TransitionSignal with confidence \u2265 0.60 and direction (long/short)")], { numbering: { reference: "numbers", level: 0 } }),
        para([txt("Volume confirmation: current bar volume \u2265 1.5\u00D7 SMA20 volume")], { numbering: { reference: "numbers", level: 0 } }),
        para([txt("ADX rising gate: ADX(14) > previous bar ADX (momentum building, not fading)")], { numbering: { reference: "numbers", level: 0 } }),
        para([txt("RSI directional filter: RSI > 50 for longs, RSI < 50 for shorts")], { numbering: { reference: "numbers", level: 0 } }),
        para([txt("4h HTF confirmation: same-direction trend on 4h timeframe (EMA20 > EMA50 for longs)")], { numbering: { reference: "numbers", level: 0 } }),
        spacer(),

        para([bold("Exit logic:")]),
        para([txt("SL: 2.0\u00D7 ATR below entry (long) / above entry (short)")], { numbering: { reference: "bullets", level: 0 } }),
        para([txt("TP: 4.0\u00D7 ATR \u2014 targeting 2:1 R:R minimum")], { numbering: { reference: "bullets", level: 0 } }),
        para([txt("Partial exit: 33% at 1R + breakeven SL (consistent with v1.2 exit logic)")], { numbering: { reference: "bullets", level: 0 } }),
        spacer(),

        para([bold("ACTIVE_REGIMES: "), txt("[] (empty) \u2014 regime control is inside evaluate() via TransitionDetector signals. The model only fires when a transition signal is active, regardless of the current regime label.")]),
        spacer(),

        heading("4.3 Integration with MomentumBreakout v2", HeadingLevel.HEADING_2),
        para([txt("TEM and MB v2 are designed as an integrated pair:")]),
        para([bold("TEM fires first"), txt(" \u2014 captures the transition moment with standard position size")], { numbering: { reference: "bullets", level: 0 } }),
        para([bold("MB v2 confirms and scales"), txt(" \u2014 if the transition develops into full volatility_expansion, MB v2 can add a second position (different symbol or scale existing)")], { numbering: { reference: "bullets", level: 0 } }),
        para([bold("No conflict"), txt(" \u2014 TEM targets transition-in-progress; MB v2 targets established expansion")], { numbering: { reference: "bullets", level: 0 } }),
        spacer(),

        heading("4.4 Config Parameters", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [3500, 1500, 4360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Parameter")], 3500), hdrCell([hdrTxt("Default")], 1500), hdrCell([hdrTxt("Notes")], 4360)
            ]}),
            ...[ ["transition_confidence_min", "0.60", "Minimum TransitionDetector confidence"],
                 ["vol_mult_min", "1.5", "Volume \u2265 1.5\u00D7 SMA20"],
                 ["adx_rising", "true", "ADX must be increasing"],
                 ["sl_atr_mult", "2.0", "Stop-loss distance in ATR"],
                 ["tp_atr_mult", "4.0", "Take-profit distance in ATR"],
                 ["htf_confirm", "true", "Require 4h trend alignment"],
                 ["cooldown_bars", "10", "Min bars between signals (from TransitionDetector)"],
                 ["strength_base", "0.40", "Base signal strength for confluence scoring"],
            ].map(([k, v, n]) => new TableRow({ children: [
              cell([para([txt(k, { font: "Courier New", size: 18 })])], 3500),
              cell([para([bold(v)])], 1500),
              cell([para([txt(n, { size: 20 })])], 4360),
            ]})),
          ]
        }),

        // ═══════════════════════════════════════════
        // 5. MODEL 2: MomentumBreakout v2
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("5. Model 2: MomentumBreakout v2 (MB v2)", HeadingLevel.HEADING_1),

        heading("5.1 Thesis", HeadingLevel.HEADING_2),
        para([txt("Session 49 proved that MomentumBreakout is profitable on SOL and ETH but structurally negative on BTC (PF=0.9163, AvgR=\u22120.021). MB v2 applies two critical restrictions: (1) asset filter excluding BTC, and (2) strict volatility_expansion regime gate. This transforms MB from an indiscriminate breakout into a precision expansion trader.")]),

        heading("5.2 Changes from MB v1", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2400, 3480, 3480],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Aspect")], 2400), hdrCell([hdrTxt("MB v1 (Session 49)")], 3480), hdrCell([hdrTxt("MB v2 (New)")], 3480)
            ]}),
            ...[ ["Assets", "BTC + SOL + ETH", "SOL + ETH only (BTC excluded)"],
                 ["Regime gate", "All regimes (ACTIVE_REGIMES=[])", "volatility_expansion only"],
                 ["Lookback", "20 (default)", "60 (Session 49 best)"],
                 ["Vol multiplier", "1.5 (default)", "2.0+ (reduce noise)"],
                 ["Orchestration", "Naive (crowded SLC)", "Research-Priority (SLC protected)"],
                 ["Scaling", "Independent", "Coordinates with TEM transitions"],
            ].map(([a, v1, v2]) => new TableRow({ children: [
              cell([para([bold(a)])], 2400),
              cell([para([txt(v1)])], 3480),
              cell([para([txt(v2, { color: "1565C0" })])], 3480),
            ]})),
          ]
        }),
        spacer(),

        heading("5.3 Integration: Transition \u2192 Expansion Pipeline", HeadingLevel.HEADING_2),
        para([txt("The core innovation of new Phase 2 is the integrated pipeline:")]),
        spacer(),
        para([txt("1. TransitionDetector detects regime shift (e.g., squeeze \u2192 expansion)")], { indent: { left: 360 } }),
        para([txt("2. TEM enters with standard position on the transition signal")], { indent: { left: 360 } }),
        para([txt("3. Expansion develops \u2014 ADX rises, Bollinger bands widen, volume sustains")], { indent: { left: 360 } }),
        para([txt("4. MB v2 confirms the expansion and enters a second position (SOL/ETH)")], { indent: { left: 360 } }),
        para([txt("5. RegimeCapitalAllocator scales both positions according to regime edge")], { indent: { left: 360 } }),
        spacer(),
        para([txt("This pipeline means: transition signals can trigger early entries, and volatility expansion confirms and scales into positions. Both are integrated, not independent.")]),

        // ═══════════════════════════════════════════
        // 6. MODEL 3: DonchianBreakout v2
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("6. Model 3: DonchianBreakout v2 (DB v2)", HeadingLevel.HEADING_1),

        heading("6.1 Thesis", HeadingLevel.HEADING_2),
        para([txt("Session 48 implemented DonchianBreakout as a research candidate. With default parameters (lookback=20, vol_mult=1.3, ACTIVE_REGIMES=[]), it generated 2,911 indiscriminate trades and collapsed to PF=1.0072 at fees. The model has a valid thesis (Donchian channel breakouts on elevated volume signal continuation) but needs strict parameter constraints to extract the edge.")]),

        heading("6.2 Tuning Targets (from Session 48 Pending Actions)", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [3000, 2000, 4360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Parameter Change")], 3000), hdrCell([hdrTxt("Value")], 2000), hdrCell([hdrTxt("Rationale")], 4360)
            ]}),
            ...[ ["lookback", "20 \u2192 40\u201360", "Wider channel = fewer false breakouts"],
                 ["vol_mult_min", "1.3 \u2192 1.8\u20132.5", "Stronger volume confirmation requirement"],
                 ["ACTIVE_REGIMES", "[] \u2192 restricted", "Only vol_expansion + bull/bear_trend"],
                 ["4h HTF gate", "none \u2192 required", "Eliminates noisy intraday fakes"],
                 ["sl_atr_mult", "1.5 \u2192 2.0", "Wider stop to avoid whipsaws"],
                 ["entry_buffer_atr", "0.10 \u2192 0.15", "Require price to break further past channel"],
            ].map(([p, v, r]) => new TableRow({ children: [
              cell([para([txt(p, { font: "Courier New", size: 18 })])], 3000),
              cell([para([bold(v)])], 2000),
              cell([para([txt(r, { size: 20 })])], 4360),
            ]})),
          ]
        }),
        spacer(),
        para([bold("Validation script: "), txt("scripts/trend_replacement/phase5_comparison.py (Scenario C) after each tuning iteration.")]),
        para([bold("Acceptance: "), txt("PF \u2265 1.18 (fees), MaxDD \u2264 25%, n \u2265 200 trades over 4-year backtest.")]),

        // ═══════════════════════════════════════════
        // 7. INFRASTRUCTURE ACTIVATION
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("7. Infrastructure Activation", HeadingLevel.HEADING_1),
        para([txt("Phase 1 built three infrastructure components that are currently config-gated. Phase 2 activates them to support the three new models:")]),
        spacer(),

        heading("7.1 RegimeCapitalAllocator", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("capital.regime_scaling_enabled: true")]),
        para([bold("Purpose: "), txt("Scales position risk% by regime edge quality. Bull_trend gets 1.2\u00D7 base risk; volatility_expansion gets 0.8\u00D7 (higher uncertainty); crisis/liquidation always 0.0\u00D7.")]),
        para([bold("Integration: "), txt("PositionSizer.calculate_risk_based() already has the call path; enabling the config flag activates it. Transition signals use a 0.60\u00D7 uncertainty discount.")]),
        spacer(),

        heading("7.2 CoverageGuarantee", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("coverage_guarantee.enabled: true")]),
        para([bold("Purpose: "), txt("Graduated fallback when scanner finds no signals for consecutive cycles. Prevents extended idle periods that miss regime shifts.")]),
        para([bold("Levels: "), txt("INFO (3 cycles) \u2192 EXPAND (6) \u2192 ENRICHMENT (12) \u2192 NOTIFY (24). Safety caps: max 2 fallback trades per 6-hour window, 0.3\u20130.5\u00D7 position size.")]),
        spacer(),

        heading("7.3 TransitionDetector (already active for TEM)", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("transition_detector.enabled: true")]),
        para([bold("Purpose: "), txt("Provides TransitionSignal objects consumed by TEM. Per-symbol instances with 10-bar cooldown prevent rapid re-triggers.")]),
        para([bold("Note: "), txt("This is activated as part of Model 1 (TEM) implementation, not as a separate infrastructure step.")]),

        // ═══════════════════════════════════════════
        // 8. IMPLEMENTATION ROADMAP
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("8. Implementation Roadmap", HeadingLevel.HEADING_1),
        para([txt("Each step is phase-gated: validation must pass before proceeding to the next step. All models remain in disabled_models until individually validated.")]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [600, 2800, 3000, 2960],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("#")], 600), hdrCell([hdrTxt("Step")], 2800),
              hdrCell([hdrTxt("Deliverable")], 3000), hdrCell([hdrTxt("Gate")], 2960)
            ]}),
            ...[ ["1", "DonchianBreakout v2 tuning", "Param grid search + IS/OOS validation", "PF\u22651.18, MaxDD\u226425%, n\u2265200"],
                 ["2", "TransitionExecutionModel build", "New model + unit tests + backtest", "PF\u22651.18 standalone, signals fire correctly"],
                 ["3", "MomentumBreakout v2 build", "Asset-filtered + regime-gated MB", "Combined PF \u2265 baseline"],
                 ["4", "Combined portfolio validation", "All 3 models + PBL/SLC together", "Portfolio PF > baseline, no SLC crowding"],
                 ["5", "Infrastructure activation", "RegimeCapitalAllocator + CoverageGuarantee", "No degradation, sizing correct"],
                 ["6", "Full regression + Stage 8", "Runtime validation on live system", "All tests pass, signals fire correctly"],
            ].map(([n, s, d, g]) => new TableRow({ children: [
              cell([para([bold(n)])], 600),
              cell([para([bold(s)])], 2800),
              cell([para([txt(d, { size: 20 })])], 3000),
              cell([para([txt(g, { size: 20 })])], 2960),
            ]})),
          ]
        }),
        spacer(),
        para([bold("Estimated effort: "), txt("6 implementation sessions. Each step produces a validation report and requires explicit approval before proceeding.")]),
        para([bold("Order rationale: "), txt("DonchianBreakout first because it already exists and just needs parameter tuning. TEM second because it enables the integration pipeline. MB v2 third because it depends on the orchestration patterns validated in steps 1\u20132.")]),

        // ═══════════════════════════════════════════
        // 9. RISK ANALYSIS
        // ═══════════════════════════════════════════
        heading("9. Risk Analysis", HeadingLevel.HEADING_1),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2500, 2500, 2200, 2160],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Risk")], 2500), hdrCell([hdrTxt("Impact")], 2500),
              hdrCell([hdrTxt("Likelihood")], 2200), hdrCell([hdrTxt("Mitigation")], 2160)
            ]}),
            ...[ ["DonchianBreakout fails tuning (like RAM)", "One model fewer; still have TEM + MB v2", "Medium", "Accept and focus on TEM/MB v2"],
                 ["TEM transition signals too rare", "Low trade count (< 200 over 4yr)", "Medium", "Relax confidence threshold; expand transition types"],
                 ["MB v2 still degrades portfolio", "Cannot add to production", "Low", "SOL/ETH + vol_expansion should fix; if not, drop MB entirely"],
                 ["SLC crowding persists", "SLC trades displaced", "Medium", "Research-Priority orchestration (tested Session 49)"],
                 ["Infrastructure activation causes regression", "Production instability", "Low", "Config-gated; full regression + Stage 8 before activation"],
            ].map(([r, i, l, m]) => new TableRow({ children: [
              cell([para([txt(r, { size: 20 })])], 2500),
              cell([para([txt(i, { size: 20 })])], 2500),
              cell([para([txt(l, { size: 20, bold: l === "Low", color: l === "Low" ? "2E7D32" : undefined })])], 2200),
              cell([para([txt(m, { size: 20 })])], 2160),
            ]})),
          ]
        }),

        // ═══════════════════════════════════════════
        // 10. SUCCESS CRITERIA
        // ═══════════════════════════════════════════
        spacer(),
        heading("10. Success Criteria", HeadingLevel.HEADING_1),
        para([txt("Phase 2 is considered successful when:")]),
        spacer(),

        para([bold("At least 2 of 3 models pass individual validation"), txt(" (PF \u2265 1.18 with fees, MaxDD \u2264 25%, n \u2265 200)")], { numbering: { reference: "numbers2", level: 0 } }),
        para([bold("Combined portfolio PF exceeds baseline"), txt(" (currently 1.441 with fees, 1.5462 zero-fee)")], { numbering: { reference: "numbers2", level: 0 } }),
        para([bold("No SLC crowding"), txt(" \u2014 SLC trade count in combined mode must be \u2265 95% of standalone SLC count")], { numbering: { reference: "numbers2", level: 0 } }),
        para([bold("Full regression passes"), txt(" \u2014 all 2,428 tests green (VM + desktop)")], { numbering: { reference: "numbers2", level: 0 } }),
        para([bold("Stage 8 runtime validation passes"), txt(" \u2014 signals fire correctly in live system")], { numbering: { reference: "numbers2", level: 0 } }),
        spacer(),
        para([txt("If only 1 model passes, Phase 2 is considered a partial success and we proceed to Phase 3 (Shadow Mode) with the validated model(s) + existing PBL/SLC baseline.")]),

        // ═══════════════════════════════════════════
        // APPENDIX: Phase Map
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("Appendix: Updated Phase Map", HeadingLevel.HEADING_1),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [1200, 3500, 2400, 2260],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Phase")], 1200), hdrCell([hdrTxt("Scope")], 3500),
              hdrCell([hdrTxt("Status")], 2400), hdrCell([hdrTxt("Gate")], 2260)
            ]}),
            new TableRow({ children: [
              cell([para([bold("1")])], 1200),
              cell([para([bold("Infrastructure"), txt("\nTransitionDetector, CoverageGuarantee, RegimeCapitalAllocator")])], 3500),
              cell([para([txt("COMPLETE", { bold: true, color: "2E7D32" })])], 2400),
              cell([para([txt("All 3 components built + gated")])], 2260),
            ]}),
            new TableRow({ children: [
              cell([para([bold("2a")])], 1200, { shading: { fill: "FFEBEE", type: ShadingType.CLEAR } }),
              cell([para([bold("Strategy Dev (RAM)"), txt("\nRangeAccumulationModel + Confluence v2")])], 3500, { shading: { fill: "FFEBEE", type: ShadingType.CLEAR } }),
              cell([para([txt("FAILED", { bold: true, color: "C62828" })])], 2400, { shading: { fill: "FFEBEE", type: ShadingType.CLEAR } }),
              cell([para([txt("Best PF=1.0633 < 1.18")])], 2260, { shading: { fill: "FFEBEE", type: ShadingType.CLEAR } }),
            ]}),
            new TableRow({ children: [
              cell([para([bold("2b")])], 1200, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
              cell([para([bold("Strategy Dev (Momentum/Transition)"), txt("\nTEM + MB v2 + DB v2 + infra activation")])], 3500, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
              cell([para([txt("THIS PLAN", { bold: true, color: "1565C0" })])], 2400, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
              cell([para([txt("Per-model + combined PF gates")])], 2260, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
            ]}),
            new TableRow({ children: [
              cell([para([bold("3")])], 1200),
              cell([para([bold("Shadow Mode"), txt("\nReal-time validation without execution")])], 3500),
              cell([para([txt("PENDING")])], 2400),
              cell([para([txt("Phase 2b validation passes")])], 2260),
            ]}),
            new TableRow({ children: [
              cell([para([bold("4")])], 1200),
              cell([para([bold("Live Deployment"), txt("\nSmall capital \u2192 scale")])], 3500),
              cell([para([txt("PENDING")])], 2400),
              cell([para([txt("Shadow mode confirms edge")])], 2260),
            ]}),
          ]
        }),
      ]
    }
  ]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/sessions/festive-optimistic-sagan/mnt/NexusTrader/reports/phase2_momentum_transition_plan.docx", buffer);
  console.log("OK: phase2_momentum_transition_plan.docx written");
});
