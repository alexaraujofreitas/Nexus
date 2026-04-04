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
const W = 9360;

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
function bullet(children) { return para(children, { numbering: { reference: "bullets", level: 0 } }); }
function bullet2(children) { return para(children, { numbering: { reference: "bullets", level: 1 } }); }
function num(children, ref) { return para(children, { numbering: { reference: ref || "numbers", level: 0 } }); }

function warnBox(children) {
  return new Table({
    width: { size: W, type: WidthType.DXA }, columnWidths: [W],
    rows: [new TableRow({ children: [
      new TableCell({
        borders: { top: { style: BorderStyle.SINGLE, size: 2, color: "E65100" },
                   bottom: { style: BorderStyle.SINGLE, size: 2, color: "E65100" },
                   left: { style: BorderStyle.SINGLE, size: 6, color: "E65100" },
                   right: { style: BorderStyle.SINGLE, size: 2, color: "E65100" } },
        width: { size: W, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 200, right: 200 },
        shading: { fill: "FFF3E0", type: ShadingType.CLEAR },
        children: Array.isArray(children) ? children : [children]
      })
    ]})]
  });
}

function infoBox(children) {
  return new Table({
    width: { size: W, type: WidthType.DXA }, columnWidths: [W],
    rows: [new TableRow({ children: [
      new TableCell({
        borders: { top: { style: BorderStyle.SINGLE, size: 2, color: "1565C0" },
                   bottom: { style: BorderStyle.SINGLE, size: 2, color: "1565C0" },
                   left: { style: BorderStyle.SINGLE, size: 6, color: "1565C0" },
                   right: { style: BorderStyle.SINGLE, size: 2, color: "1565C0" } },
        width: { size: W, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 200, right: 200 },
        shading: { fill: "E3F2FD", type: ShadingType.CLEAR },
        children: Array.isArray(children) ? children : [children]
      })
    ]})]
  });
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
      { reference: "numbers5", levels: [
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
        para([txt("Phase 2b Strategy Development Plan", { size: 36, color: "2E5984" })], { alignment: AlignmentType.CENTER }),
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
        para([txt("Session 51  |  March 30, 2026  |  v2.1 (with required adjustments)", { size: 22, color: "777777" })], { alignment: AlignmentType.CENTER }),
        spacer(), spacer(), spacer(), spacer(), spacer(), spacer(),
        // Status box
        new Table({
          width: { size: 6000, type: WidthType.DXA },
          columnWidths: [6000],
          rows: [new TableRow({ children: [
            new TableCell({
              borders: { top: { style: BorderStyle.SINGLE, size: 2, color: "2E7D32" },
                         bottom: { style: BorderStyle.SINGLE, size: 2, color: "2E7D32" },
                         left: { style: BorderStyle.SINGLE, size: 6, color: "2E7D32" },
                         right: { style: BorderStyle.SINGLE, size: 2, color: "2E7D32" } },
              width: { size: 6000, type: WidthType.DXA }, margins: { top: 120, bottom: 120, left: 200, right: 200 },
              shading: { fill: "E8F5E9", type: ShadingType.CLEAR },
              children: [
                para([bold("STATUS: "), txt("APPROVED WITH ADJUSTMENTS", { color: "2E7D32", bold: true })]),
                para([bold("SCOPE: "), txt("3 models + infrastructure activation + conflict resolution")]),
                para([bold("BASELINE: "), txt("PBL+SLC PF=1.441 (fees) / 1.5462 (zero-fee), n=1,412")]),
                para([bold("CONSTRAINT: "), txt("No mean-reversion. Momentum & transition only.")]),
                para([bold("v2.1 CHANGES: "), txt("Signal density pre-test, DB v2 experimental gate, tightened combined validation, conflict resolution hierarchy, TEM trailing stop")]),
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
              txt("NexusTrader Phase 2b Plan v2.1", { size: 18, color: "999999" }),
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
        para([txt("This plan replaces the failed Phase 2 with a ", { size: 22 }), bold("momentum/transition-focused system", { size: 22 }), txt(" comprising three integrated models, targeted infrastructure activation, and explicit conflict resolution:", { size: 22 })]),
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
              cell([para([txt("Trades validated regime transitions. Signal density pre-test required (\u2265200 signals/4yr). Breakout continuation exits with trailing stop.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("2")])], 500),
              cell([para([bold("MomentumBreakout v2")])], 2200),
              cell([para([txt("Refined MB: SOL/ETH only, vol_expansion gate, confirms and scales into TEM positions.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("3")])], 500),
              cell([para([bold("DonchianBreakout v2")])], 2200),
              cell([para([txt("EXPERIMENTAL. Strict params + 4h HTF gate. PF < 1.18 at fees \u2192 immediate drop, no extended tuning.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("4")])], 500),
              cell([para([bold("Conflict Resolution")])], 2200),
              cell([para([txt("Model priority hierarchy (SLC > PBL > TEM > MB v2 > DB v2). Max 3 concurrent positions, 1 per asset. SLC crowding prevention.")])], 6660),
            ]}),
            new TableRow({ children: [
              cell([para([bold("5")])], 500),
              cell([para([bold("Infrastructure")])], 2200),
              cell([para([txt("Activate RegimeCapitalAllocator and CoverageGuarantee. Research-Priority orchestration.")])], 6660),
            ]}),
          ]
        }),
        spacer(),
        para([bold("Acceptance criteria (per model): "), txt("PF \u2265 1.18 with fees (0.04%/side), MaxDD \u2264 25%, n \u2265 200.")]),
        para([bold("Combined portfolio gate: "), txt("PF \u2265 baseline + 0.03 (i.e. \u2265 1.471), OR no MaxDD increase AND no SLC PF degradation.")]),
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
            new TableRow({ children: [
              cell([para([bold("Extended tuning trap")])], 2500),
              cell([para([txt("RAM consumed 3+ sessions of tuning with diminishing returns. NEW RULE: If a model fails its first validation pass, drop it immediately. No extended tuning cycles.")])], 6860),
            ]}),
          ]
        }),

        // ═══════════════════════════════════════════
        // 3. CURRENT SYSTEM BASELINE
        // ═══════════════════════════════════════════
        spacer(),
        heading("3. Current System Baseline", HeadingLevel.HEADING_1),
        para([txt("All new models must improve upon this baseline. The combined portfolio gate is strict: PF must exceed baseline by +0.03, or the model must cause no MaxDD increase and no SLC PF degradation.")]),
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
              cell([para([bold("BASELINE")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("56.4%")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1.5462")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1.441")])], 1200, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("1,412")])], 1280, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
              cell([para([bold("\u2014")])], 1280, { shading: { fill: "E8F5E9", type: ShadingType.CLEAR } }),
            ]}),
          ]
        }),
        spacer(),
        para([txt("Session 50 optimized parameters: sl=3.0\u00D7ATR, tp=4.0\u00D7ATR, ema_prox=0.4, rsi_min=45, wick_strength=1.5.")]),
        spacer(),

        infoBox([
          para([bold("Combined Portfolio Validation Gate (v2.1)", { size: 22 })]),
          para([txt("Any new model added to production must satisfy at least ONE of:")]),
          para([bold("Option A: "), txt("Combined PF(fees) \u2265 1.471 (baseline 1.441 + 0.03)")]),
          para([bold("Option B: "), txt("MaxDD does not increase beyond baseline AND SLC standalone PF is not degraded")]),
          para([txt("If neither gate is met, the model is rejected regardless of standalone quality.", { italics: true })]),
        ]),

        // ═══════════════════════════════════════════
        // 4. MODEL 1: TransitionExecutionModel
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("4. Model 1: TransitionExecutionModel (TEM)", HeadingLevel.HEADING_1),

        heading("4.1 Thesis", HeadingLevel.HEADING_2),
        para([txt("Regime transitions are the highest-edge moments in crypto markets. When price shifts from ranging to trending (or from compression to expansion), directional momentum is strongest and most predictable. The TransitionExecutionModel captures this edge by entering at the moment of validated transition, before the trend is fully established.")]),

        heading("4.2 Signal Density Pre-Test (MANDATORY)", HeadingLevel.HEADING_2),
        spacer(),
        warnBox([
          para([bold("REQUIRED: Run signal frequency analysis BEFORE full backtest", { size: 22, color: "E65100" })]),
          para([txt("Target: \u2265 200\u2013400 raw transition signals over the 4-year dataset (BTC+SOL+ETH, 30m).")]),
          para([txt("Methodology: Run TransitionDetector over the full 4-year dataset with default parameters. Count raw signals by type (BREAKOUT, EXPANSION, BREAKDOWN, TREND_FORMING) per symbol.")]),
          para([bold("If signal count < 200:"), txt(" relax exactly ONE parameter:")]),
          para([txt("Option 1: Lower transition_confidence_min from 0.60 \u2192 0.50 (test first)"), { indent: { left: 360 } }]),
          para([txt("Option 2: Lower vol_mult_min from 1.5 \u2192 1.2 (test second)"), { indent: { left: 360 } }]),
          para([txt("Never relax both simultaneously. Re-run density test after each change.", { italics: true })]),
          para([bold("If signal count < 100 even after relaxation:"), txt(" TEM is structurally non-viable at 30m. Abort and skip to MB v2.")]),
        ]),
        spacer(),

        heading("4.3 Architecture", HeadingLevel.HEADING_2),
        para([bold("Signal source: "), txt("TransitionDetector (Phase 1 infrastructure, already implemented)")]),
        para([bold("Transition types targeted:")]),

        bullet([bold("TRANSITION_BREAKOUT"), txt(" \u2014 ranging \u2192 bull_trend or bear_trend (5-bar duration)")]),
        bullet([bold("TRANSITION_EXPANSION"), txt(" \u2014 vol_compression / squeeze \u2192 vol_expansion (4-bar duration)")]),
        bullet([bold("TRANSITION_TREND_FORMING"), txt(" \u2014 uncertain / accumulation \u2192 bull_trend (3-bar duration)")]),
        spacer(),

        para([bold("Entry logic:")]),
        num([txt("TransitionDetector fires TransitionSignal with confidence \u2265 0.60 and direction (long/short)")], "numbers"),
        num([txt("Volume confirmation: current bar volume \u2265 1.5\u00D7 SMA20 volume")], "numbers"),
        num([txt("ADX rising gate: ADX(14) > previous bar ADX (momentum building, not fading)")], "numbers"),
        num([txt("RSI directional filter: RSI > 50 for longs, RSI < 50 for shorts")], "numbers"),
        num([txt("4h HTF confirmation: same-direction trend on 4h timeframe (EMA20 > EMA50 for longs)")], "numbers"),
        spacer(),

        heading("4.4 Exit Logic (Enhanced with Trailing Stop)", HeadingLevel.HEADING_2),
        para([txt("TEM uses a two-phase exit system that adapts to the strength of the expansion:")]),
        spacer(),

        para([bold("Phase 1: Standard fixed exits")]),
        bullet([txt("SL: 2.0\u00D7 ATR below entry (long) / above entry (short)")]),
        bullet([txt("TP: 4.0\u00D7 ATR \u2014 targeting 2:1 R:R minimum")]),
        bullet([txt("Partial exit: 33% at 1R + move SL to breakeven (consistent with v1.2 exit logic)")]),
        spacer(),

        para([bold("Phase 2: Breakout continuation (activated after partial exit)")]),
        bullet([txt("Condition: After 1R partial, if ADX > 30 AND BBWidth > 1.5\u00D7 SMA20(BBWidth), expansion is strong")]),
        bullet([txt("Action: Replace fixed TP with trailing stop at 1.5\u00D7 ATR behind price")]),
        bullet([txt("Trail update: Every bar, if price has moved favorably, trail = max(current_trail, close \u2212 1.5\u00D7ATR)")]),
        bullet([txt("Trail floor: Trailing stop never moves below breakeven (entry price)")]),
        bullet([txt("Deactivation: If ADX drops below 25 (expansion fading), revert to fixed TP at current price + 1.0\u00D7ATR")]),
        spacer(),

        infoBox([
          para([bold("Trailing Stop Logic Summary", { size: 22 })]),
          para([txt("strong expansion (ADX>30 + wide BB) \u2192 trail at 1.5\u00D7ATR behind price")]),
          para([txt("fading expansion (ADX<25) \u2192 lock in gains with tight TP")]),
          para([txt("normal conditions \u2192 standard fixed TP at 4.0\u00D7ATR")]),
        ]),
        spacer(),

        para([bold("ACTIVE_REGIMES: "), txt("[] (empty) \u2014 regime control is inside evaluate() via TransitionDetector signals. The model only fires when a transition signal is active, regardless of the current regime label.")]),
        spacer(),

        heading("4.5 Integration with MomentumBreakout v2", HeadingLevel.HEADING_2),
        para([txt("TEM and MB v2 are designed as an integrated pair:")]),
        bullet([bold("TEM fires first"), txt(" \u2014 captures the transition moment with standard position size")]),
        bullet([bold("MB v2 confirms and scales"), txt(" \u2014 if the transition develops into full volatility_expansion, MB v2 can add a second position (different asset, per conflict resolution rules)")]),
        bullet([bold("No conflict"), txt(" \u2014 TEM targets transition-in-progress; MB v2 targets established expansion. Per-asset limit of 1 prevents double-stacking.")]),
        spacer(),

        heading("4.6 Config Parameters", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [3500, 1500, 4360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Parameter")], 3500), hdrCell([hdrTxt("Default")], 1500), hdrCell([hdrTxt("Notes")], 4360)
            ]}),
            ...[ ["transition_confidence_min", "0.60", "Min TransitionDetector confidence (relaxable to 0.50 per density test)"],
                 ["vol_mult_min", "1.5", "Volume \u2265 1.5\u00D7 SMA20 (relaxable to 1.2 per density test)"],
                 ["adx_rising", "true", "ADX must be increasing"],
                 ["sl_atr_mult", "2.0", "Stop-loss distance in ATR"],
                 ["tp_atr_mult", "4.0", "Take-profit distance in ATR"],
                 ["htf_confirm", "true", "Require 4h trend alignment"],
                 ["cooldown_bars", "10", "Min bars between signals (from TransitionDetector)"],
                 ["strength_base", "0.40", "Base signal strength for confluence scoring"],
                 ["trail_atr_mult", "1.5", "Trailing stop distance (Phase 2 exit)"],
                 ["trail_adx_min", "30.0", "ADX threshold to activate trailing stop"],
                 ["trail_adx_deactivate", "25.0", "ADX below this reverts to fixed TP"],
                 ["trail_bbw_mult", "1.5", "BBWidth must exceed 1.5\u00D7 SMA20(BBWidth) for trail activation"],
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
                 ["Regime gate", "All regimes (ACTIVE_REGIMES=[])", "volatility_expansion only (already set)"],
                 ["Lookback", "20 (default)", "60 (Session 49 best)"],
                 ["Vol multiplier", "1.5 (default)", "2.0+ (reduce noise)"],
                 ["Orchestration", "Naive (crowded SLC)", "Research-Priority (SLC protected)"],
                 ["Scaling", "Independent", "Coordinates with TEM transitions"],
                 ["Conflict gate", "None", "Per-asset limit + priority hierarchy"],
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
        para([txt("4. MB v2 confirms the expansion and enters a second position (SOL/ETH, different asset than TEM)")], { indent: { left: 360 } }),
        para([txt("5. TEM activates trailing stop; MB v2 rides the continuation")], { indent: { left: 360 } }),
        spacer(),
        para([txt("This pipeline means: transition signals trigger early entries, and volatility expansion confirms and scales into positions. Both are integrated, not independent.")]),

        // ═══════════════════════════════════════════
        // 6. MODEL 3: DonchianBreakout v2 (EXPERIMENTAL)
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("6. Model 3: DonchianBreakout v2 (DB v2) \u2014 EXPERIMENTAL", HeadingLevel.HEADING_1),
        spacer(),

        warnBox([
          para([bold("EXPERIMENTAL MODEL \u2014 STRICT DROP POLICY", { size: 22, color: "C62828" })]),
          para([txt("DonchianBreakout v2 is treated as experimental. If the first tuning pass produces PF < 1.18 at production fees, the model is dropped immediately. No extended tuning cycles. No second chances.")]),
          para([txt("Rationale: Session 48 showed DB v1 collapsed to PF=1.0072 at fees with 2,911 indiscriminate trades. The thesis (channel breakout on volume) may be structurally unviable at 30m with fees, similar to RAM.")]),
        ]),
        spacer(),

        heading("6.1 Thesis", HeadingLevel.HEADING_2),
        para([txt("Session 48 implemented DonchianBreakout as a research candidate. With default parameters (lookback=20, vol_mult=1.3, ACTIVE_REGIMES=[]), it generated 2,911 indiscriminate trades and collapsed to PF=1.0072 at fees. The model has a valid thesis (Donchian channel breakouts on elevated volume signal continuation) but needs strict parameter constraints to extract the edge.")]),

        heading("6.2 Tuning Parameters (Single Pass)", HeadingLevel.HEADING_2),
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
        para([bold("Validation script: "), txt("scripts/trend_replacement/phase5_comparison.py (Scenario C) after tuning.")]),
        para([bold("Acceptance: "), txt("PF \u2265 1.18 (fees), MaxDD \u2264 25%, n \u2265 200 trades over 4-year backtest.")]),
        para([bold("Drop rule: "), txt("If no parameter combination in the single tuning pass achieves PF \u2265 1.18 at fees, DB v2 is permanently dropped from Phase 2b scope. The remaining two models (TEM + MB v2) proceed.", { color: "C62828" })]),

        // ═══════════════════════════════════════════
        // 7. CONFLICT RESOLUTION (NEW v2.1)
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("7. Conflict Resolution", HeadingLevel.HEADING_1),
        para([txt("When multiple models generate signals simultaneously, explicit rules govern which signals are accepted and how positions are managed.")]),
        spacer(),

        heading("7.1 Model Priority Hierarchy", HeadingLevel.HEADING_2),
        para([txt("When signals conflict (same asset, same direction, same bar), the highest-priority model wins:")]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [800, 2800, 2400, 3360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Rank")], 800), hdrCell([hdrTxt("Model")], 2800),
              hdrCell([hdrTxt("Priority Score")], 2400), hdrCell([hdrTxt("Rationale")], 3360)
            ]}),
            ...[ ["1", "SwingLowContinuation", "1.00", "Highest PF (1.5455), most consistent edge"],
                 ["2", "PullbackLong", "0.90", "Combined system anchor, validated Session 50"],
                 ["3", "TransitionExecutionModel", "0.80", "New \u2014 validated in backtest but unproven live"],
                 ["4", "MomentumBreakout v2", "0.70", "Restricted (SOL/ETH only), expansion-only"],
                 ["5", "DonchianBreakout v2", "0.60", "Experimental, lowest priority"],
            ].map(([r, m, p, rat]) => new TableRow({ children: [
              cell([para([bold(r)])], 800),
              cell([para([bold(m)])], 2800),
              cell([para([txt(p)])], 2400),
              cell([para([txt(rat, { size: 20 })])], 3360),
            ]})),
          ]
        }),
        spacer(),

        heading("7.2 Position Limits", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [4000, 2000, 3360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Constraint")], 4000), hdrCell([hdrTxt("Limit")], 2000), hdrCell([hdrTxt("Enforcement")], 3360)
            ]}),
            ...[ ["Max concurrent positions (portfolio)", "3", "Hard cap in submit() gate"],
                 ["Max positions per asset", "1", "Per-symbol check before entry"],
                 ["Max new Phase 2b positions", "2", "TEM + MB v2 or TEM + DB v2, not all 3 simultaneously"],
                 ["Min distance between entries (same asset)", "5 bars", "Prevents rapid re-entry after exit"],
            ].map(([c, l, e]) => new TableRow({ children: [
              cell([para([txt(c)])], 4000),
              cell([para([bold(l)])], 2000),
              cell([para([txt(e, { size: 20 })])], 3360),
            ]})),
          ]
        }),
        spacer(),

        heading("7.3 SLC Crowding Prevention", HeadingLevel.HEADING_2),
        para([txt("Session 49 demonstrated that naive orchestration displaces SLC trades (195 fewer SLC trades in combined mode). The following rules prevent this:")]),
        spacer(),
        bullet([bold("Research-Priority orchestration: "), txt("SLC and PBL signals are evaluated FIRST in every scan cycle. Only after existing model slots are filled do Phase 2b models (TEM, MB v2, DB v2) compete for remaining slots.")]),
        bullet([bold("SLC trade count monitoring: "), txt("During combined backtest validation, SLC standalone n must be \u2265 95% of its baseline n (currently 1,129). If SLC drops below this threshold, the offending Phase 2b model is rejected.")]),
        bullet([bold("Same-asset priority: "), txt("If SLC and a Phase 2b model both signal on the same asset at the same bar, SLC always wins (priority rank 1 vs 3\u20135).")]),
        spacer(),

        heading("7.4 Signal Conflict Resolution Rules", HeadingLevel.HEADING_2),
        num([txt("Same asset, same direction, same bar: Highest-priority model wins. Lower-priority signal is discarded.")], "numbers3"),
        num([txt("Same asset, opposite directions: Both signals are discarded (ambiguity = no trade).")], "numbers3"),
        num([txt("Different assets, same bar: Both signals can fire (up to max concurrent limit).")], "numbers3"),
        num([txt("TEM + MB v2 pipeline: TEM on asset A, MB v2 on asset B is allowed. TEM and MB v2 on the SAME asset requires TEM to already be in position (MB v2 does not open new position on same asset).")], "numbers3"),
        spacer(),

        // ═══════════════════════════════════════════
        // 8. INFRASTRUCTURE ACTIVATION
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("8. Infrastructure Activation", HeadingLevel.HEADING_1),
        para([txt("Phase 1 built three infrastructure components that are currently config-gated. Phase 2 activates them to support the three new models:")]),
        spacer(),

        heading("8.1 RegimeCapitalAllocator", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("capital.regime_scaling_enabled: true")]),
        para([bold("Purpose: "), txt("Scales position risk% by regime edge quality. Bull_trend gets 1.2\u00D7 base risk; volatility_expansion gets 0.8\u00D7 (higher uncertainty); crisis/liquidation always 0.0\u00D7.")]),
        para([bold("Integration: "), txt("PositionSizer.calculate_risk_based() already has the call path; enabling the config flag activates it. Transition signals use a 0.60\u00D7 uncertainty discount.")]),
        spacer(),

        heading("8.2 CoverageGuarantee", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("coverage_guarantee.enabled: true")]),
        para([bold("Purpose: "), txt("Graduated fallback when scanner finds no signals for consecutive cycles. Prevents extended idle periods that miss regime shifts.")]),
        para([bold("Levels: "), txt("INFO (3 cycles) \u2192 EXPAND (6) \u2192 ENRICHMENT (12) \u2192 NOTIFY (24). Safety caps: max 2 fallback trades per 6-hour window, 0.3\u20130.5\u00D7 position size.")]),
        spacer(),

        heading("8.3 TransitionDetector (already active for TEM)", HeadingLevel.HEADING_2),
        para([bold("Config gate: "), txt("transition_detector.enabled: true")]),
        para([bold("Purpose: "), txt("Provides TransitionSignal objects consumed by TEM. Per-symbol instances with 10-bar cooldown prevent rapid re-triggers.")]),
        para([bold("Note: "), txt("This is activated as part of Model 1 (TEM) implementation, not as a separate infrastructure step.")]),

        // ═══════════════════════════════════════════
        // 9. IMPLEMENTATION ROADMAP
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("9. Implementation Roadmap", HeadingLevel.HEADING_1),
        para([txt("Each step is phase-gated: validation must pass before proceeding to the next step. All models remain in disabled_models until individually validated.")]),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [500, 2600, 3200, 3060],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("#")], 500), hdrCell([hdrTxt("Step")], 2600),
              hdrCell([hdrTxt("Deliverable")], 3200), hdrCell([hdrTxt("Gate")], 3060)
            ]}),
            ...[ ["1", "DonchianBreakout v2 tuning (EXPERIMENTAL)", "Single-pass grid search + validation", "PF\u22651.18 at fees, MaxDD\u226425%, n\u2265200. FAIL \u2192 drop."],
                 ["2", "TEM signal density pre-test", "Run TransitionDetector over 4yr dataset. Count signals by type/symbol.", "\u2265200 signals. If <200, relax ONE param. If <100, abort TEM."],
                 ["3", "TEM build + backtest", "New model + unit tests + standalone backtest + trailing stop logic", "PF\u22651.18 standalone, signals fire correctly, trail logic verified"],
                 ["4", "MomentumBreakout v2 build", "Asset-filtered + regime-gated MB, SOL/ETH only", "Standalone PF\u22651.18 on SOL/ETH"],
                 ["5", "Combined portfolio validation", "All passing models + PBL/SLC together + conflict resolution", "PF \u2265 baseline+0.03 OR (no MaxDD increase + no SLC PF degradation)"],
                 ["6", "Infrastructure activation", "RegimeCapitalAllocator + CoverageGuarantee", "No degradation, sizing correct"],
                 ["7", "Full regression + Stage 8", "Runtime validation on live system", "All tests pass, signals fire correctly"],
            ].map(([n, s, d, g]) => new TableRow({ children: [
              cell([para([bold(n)])], 500),
              cell([para([bold(s)])], 2600),
              cell([para([txt(d, { size: 20 })])], 3200),
              cell([para([txt(g, { size: 20 })])], 3060),
            ]})),
          ]
        }),
        spacer(),
        para([bold("Estimated effort: "), txt("7 implementation sessions. Each step produces a validation report and requires explicit approval before proceeding.")]),
        para([bold("Order rationale: "), txt("DonchianBreakout first (experimental, quick pass/fail). TEM density pre-test before building the model. MB v2 depends on TEM orchestration patterns. Combined validation last.")]),
        spacer(),
        para([bold("Early termination rules:")]),
        bullet([txt("DB v2 fails Step 1 \u2192 skip, proceed to Step 2. Phase 2b continues with TEM + MB v2.")]),
        bullet([txt("TEM fails density test (Step 2, <100 signals after relaxation) \u2192 skip Steps 2\u20133. Phase 2b continues with MB v2 only.")]),
        bullet([txt("Both DB v2 and TEM fail \u2192 Phase 2b reduces to MB v2 standalone evaluation against baseline.")]),
        bullet([txt("All three fail \u2192 Phase 2b is declared failed. Proceed to Phase 3 (Shadow Mode) with existing PBL+SLC only.")]),

        // ═══════════════════════════════════════════
        // 10. RISK ANALYSIS
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("10. Risk Analysis", HeadingLevel.HEADING_1),
        spacer(),

        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2500, 2500, 1400, 2960],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Risk")], 2500), hdrCell([hdrTxt("Impact")], 2500),
              hdrCell([hdrTxt("Likelihood")], 1400), hdrCell([hdrTxt("Mitigation")], 2960)
            ]}),
            ...[ ["DonchianBreakout fails tuning", "One model fewer; still have TEM + MB v2", "High", "Drop immediately (v2.1 rule). No extended tuning."],
                 ["TEM transition signals too rare (<200)", "Cannot build TEM; lose integrated pipeline", "Medium", "Relax ONE param. If <100, abort TEM."],
                 ["TEM trailing stop over-fitted", "Backtest looks good but live edge vanishes", "Low", "OOS validation required. Trail params conservative."],
                 ["MB v2 still degrades portfolio", "Cannot add to production", "Low", "SOL/ETH + vol_expansion gate. If fails, drop."],
                 ["SLC crowding persists", "SLC trades displaced = portfolio degradation", "Medium", "Research-Priority + per-asset limits + SLC monitoring."],
                 ["All three models fail", "Phase 2b produces no new models", "Low", "PBL+SLC baseline is strong (PF=1.441). Proceed to Phase 3."],
                 ["Infrastructure activation regresses", "Production instability", "Low", "Config-gated; full regression + Stage 8 validation."],
            ].map(([r, i, l, m]) => new TableRow({ children: [
              cell([para([txt(r, { size: 20 })])], 2500),
              cell([para([txt(i, { size: 20 })])], 2500),
              cell([para([txt(l, { size: 20, bold: true, color: l === "Low" ? "2E7D32" : l === "High" ? "C62828" : "E65100" })])], 1400),
              cell([para([txt(m, { size: 20 })])], 2960),
            ]})),
          ]
        }),

        // ═══════════════════════════════════════════
        // 11. SUCCESS CRITERIA
        // ═══════════════════════════════════════════
        spacer(),
        heading("11. Success Criteria", HeadingLevel.HEADING_1),
        para([txt("Phase 2b is considered successful when:")]),
        spacer(),

        num([bold("At least 2 of 3 models pass individual validation"), txt(" (PF \u2265 1.18 with fees, MaxDD \u2264 25%, n \u2265 200)")], "numbers4"),
        num([bold("Combined portfolio exceeds baseline quality:"), txt(" PF(fees) \u2265 1.471 (baseline + 0.03), OR no increase in MaxDD AND no degradation in SLC PF")], "numbers4"),
        num([bold("No SLC crowding"), txt(" \u2014 SLC trade count in combined mode must be \u2265 95% of standalone SLC count (1,129)")], "numbers4"),
        num([bold("Conflict resolution validated"), txt(" \u2014 priority hierarchy, per-asset limits, and position caps all enforced in backtest")], "numbers4"),
        num([bold("Full regression passes"), txt(" \u2014 all 2,428+ tests green (VM + desktop)")], "numbers4"),
        num([bold("Stage 8 runtime validation passes"), txt(" \u2014 signals fire correctly in live system")], "numbers4"),
        spacer(),
        para([txt("If only 1 model passes, Phase 2b is considered a partial success and we proceed to Phase 3 (Shadow Mode) with the validated model(s) + existing PBL/SLC baseline.")]),
        para([txt("If 0 models pass, Phase 2b is declared failed. PBL+SLC baseline carries forward unchanged.")]),

        // ═══════════════════════════════════════════
        // 12. SIGNAL DENSITY TEST APPROACH
        // ═══════════════════════════════════════════
        new Paragraph({ children: [new PageBreak()] }),
        heading("12. Signal Density Test Approach (TEM Pre-Requisite)", HeadingLevel.HEADING_1),
        para([txt("This section details the methodology for the mandatory TEM signal density pre-test (Implementation Roadmap Step 2).")]),
        spacer(),

        heading("12.1 Test Design", HeadingLevel.HEADING_2),
        para([bold("Dataset: "), txt("Same 4-year BTC+SOL+ETH 30m dataset used in all backtests (2022-03-22 \u2192 2026-03-21)")]),
        para([bold("Tool: "), txt("Standalone script: scripts/tem_research/signal_density_test.py")]),
        spacer(),

        para([bold("Methodology:")]),
        num([txt("Load OHLCV data for all 3 symbols (30m bars, ~70,079 bars each)")], "numbers5"),
        num([txt("Instantiate TransitionDetector per symbol (default params)")], "numbers5"),
        num([txt("For each bar: compute HMM regime probs + technical features, call detector.detect()")], "numbers5"),
        num([txt("Record every TransitionSignal: timestamp, symbol, type, confidence, direction")], "numbers5"),
        num([txt("Aggregate: total signals, signals by type, signals by symbol, signals per year")], "numbers5"),
        spacer(),

        heading("12.2 Pass/Fail Criteria", HeadingLevel.HEADING_2),
        new Table({
          width: { size: W, type: WidthType.DXA },
          columnWidths: [2500, 2500, 4360],
          rows: [
            new TableRow({ children: [
              hdrCell([hdrTxt("Signal Count")], 2500), hdrCell([hdrTxt("Verdict")], 2500), hdrCell([hdrTxt("Action")], 4360)
            ]}),
            new TableRow({ children: [
              cell([para([bold("\u2265 400")])], 2500),
              cell([para([txt("PASS (strong)", { color: "2E7D32", bold: true })])], 2500),
              cell([para([txt("Proceed to TEM build with default params")])], 4360),
            ]}),
            new TableRow({ children: [
              cell([para([bold("200\u2013399")])], 2500),
              cell([para([txt("PASS (adequate)", { color: "2E7D32", bold: true })])], 2500),
              cell([para([txt("Proceed. May need relaxed params if backtest n < 200 after filtering")])], 4360),
            ]}),
            new TableRow({ children: [
              cell([para([bold("100\u2013199")])], 2500),
              cell([para([txt("MARGINAL", { color: "E65100", bold: true })])], 2500),
              cell([para([txt("Relax ONE param (confidence \u2192 0.50 OR vol_mult \u2192 1.2). Retest.")])], 4360),
            ]}),
            new TableRow({ children: [
              cell([para([bold("< 100")])], 2500),
              cell([para([txt("FAIL", { color: "C62828", bold: true })])], 2500),
              cell([para([txt("If already relaxed, abort TEM entirely. Skip to MB v2.")])], 4360),
            ]}),
          ]
        }),
        spacer(),

        heading("12.3 Expected Signal Distribution", HeadingLevel.HEADING_2),
        para([txt("Based on TransitionDetector implementation analysis:")]),
        bullet([bold("TRANSITION_BREAKOUT: "), txt("Requires accumulation_drop \u2265 0.12 + ADX rising + volume_trend true. Moderate frequency.")]),
        bullet([bold("TRANSITION_EXPANSION: "), txt("Requires BB width increase \u2265 30% over 3 bars + volume confirmation. Less frequent (compression \u2192 expansion is specific).")]),
        bullet([bold("TRANSITION_BREAKDOWN: "), txt("Mirror of BREAKOUT for bear direction. Requires distribution_drop \u2265 0.12.")]),
        bullet([bold("TRANSITION_TREND_FORMING: "), txt("Requires recovery/uncertain \u2192 bull with EMA slope positive. Most common transition type.")]),
        spacer(),
        para([txt("The 10-bar per-type cooldown limits maximum theoretical signals to ~6,000 per symbol over 4 years (70,079 bars / 10 cooldown \u00D7 0.85 utilization). Practical count depends on how often regime conditions align.")]),

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
              cell([para([bold("Strategy Dev (Momentum/Transition)"), txt("\nTEM + MB v2 + DB v2 + conflict resolution + infra")])], 3500, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
              cell([para([txt("APPROVED", { bold: true, color: "1565C0" })])], 2400, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
              cell([para([txt("Per-model + combined PF gates + v2.1 adjustments")])], 2260, { shading: { fill: "E3F2FD", type: ShadingType.CLEAR } }),
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
        spacer(),
        spacer(),
        para([txt("End of Document", { italics: true, color: "999999" })], { alignment: AlignmentType.CENTER }),
      ]
    }
  ]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/sessions/festive-optimistic-sagan/mnt/NexusTrader/reports/phase2_momentum_transition_plan_v2.docx", buffer);
  console.log("OK: phase2_momentum_transition_plan_v2.docx written");
});
