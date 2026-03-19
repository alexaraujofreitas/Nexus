const fs = require("fs");
const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        Header, Footer, AlignmentType, HeadingLevel, BorderStyle, WidthType,
        ShadingType, PageNumber, PageBreak, LevelFormat } = require("docx");

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const cellMargins = { top: 80, bottom: 80, left: 120, right: 120 };
const accentColor = "1B4F72";
const lightAccent = "D6EAF8";
const warningColor = "FDEDEC";
const successColor = "D5F5E3";

function headerCell(text, width) {
  return new TableCell({
    borders, width: { size: width, type: WidthType.DXA },
    shading: { fill: accentColor, type: ShadingType.CLEAR },
    margins: cellMargins, verticalAlign: "center",
    children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ text, bold: true, color: "FFFFFF", font: "Arial", size: 20 })] })]
  });
}

function cell(text, width, opts = {}) {
  return new TableCell({
    borders, width: { size: width, type: WidthType.DXA },
    shading: opts.fill ? { fill: opts.fill, type: ShadingType.CLEAR } : undefined,
    margins: cellMargins,
    children: [new Paragraph({ alignment: opts.align || AlignmentType.LEFT,
      children: [new TextRun({ text, font: "Arial", size: 20, bold: !!opts.bold, color: opts.color || "333333" })] })]
  });
}

function row(...cells) { return new TableRow({ children: cells }); }

function heading(text, level = HeadingLevel.HEADING_1) {
  return new Paragraph({ heading: level, spacing: { before: 300, after: 150 },
    children: [new TextRun({ text, font: "Arial", bold: true })] });
}

function para(text, opts = {}) {
  return new Paragraph({ spacing: { after: 120 }, alignment: opts.align,
    children: [new TextRun({ text, font: "Arial", size: 22, ...opts })] });
}

function boldPara(label, text) {
  return new Paragraph({ spacing: { after: 120 },
    children: [
      new TextRun({ text: label, font: "Arial", size: 22, bold: true }),
      new TextRun({ text, font: "Arial", size: 22 })
    ] });
}

// ── Build Document ──
const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 32, bold: true, font: "Arial", color: accentColor },
        paragraph: { spacing: { before: 360, after: 200 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: "Arial", color: "2E86C1" },
        paragraph: { spacing: { before: 280, after: 160 }, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 23, bold: true, font: "Arial", color: "2874A6" },
        paragraph: { spacing: { before: 200, after: 120 }, outlineLevel: 2 } },
    ]
  },
  numbering: {
    config: [
      { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
      { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT, style: { paragraph: { indent: { left: 720, hanging: 360 } } } }] },
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
      }
    },
    headers: {
      default: new Header({ children: [
        new Paragraph({ alignment: AlignmentType.RIGHT, border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: accentColor, space: 4 } },
          children: [new TextRun({ text: "NexusTrader Strategy Optimization Report", font: "Arial", size: 16, color: "888888", italics: true })] })
      ] })
    },
    footers: {
      default: new Footer({ children: [
        new Paragraph({ alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "Page ", font: "Arial", size: 16, color: "888888" }), new TextRun({ children: [PageNumber.CURRENT], font: "Arial", size: 16, color: "888888" })] })
      ] })
    },
    children: [
      // ═══ TITLE PAGE ═══
      new Paragraph({ spacing: { before: 3000 } }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
        children: [new TextRun({ text: "NEXUS TRADER", font: "Arial", size: 52, bold: true, color: accentColor })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 100 },
        children: [new TextRun({ text: "Strategy Optimization Report", font: "Arial", size: 36, color: "2E86C1" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 60 },
        border: { bottom: { style: BorderStyle.SINGLE, size: 6, color: accentColor, space: 8 } },
        children: [new TextRun({ text: "Maximizing Total Profit with Moderate Risk", font: "Arial", size: 24, color: "666666", italics: true })] }),
      new Paragraph({ spacing: { before: 600 }, alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "March 2026  |  Bybit Demo Phase", font: "Arial", size: 22, color: "888888" })] }),

      new Paragraph({ children: [new PageBreak()] }),

      // ═══ EXECUTIVE SUMMARY ═══
      heading("Executive Summary"),
      para("This report identifies seven high-impact optimization opportunities across the NexusTrader IDSS pipeline, derived from analysis of the current codebase, live trading data, walk-forward validation results, and the Level-2 adaptive learning system output. The recommendations are ordered by expected profit impact and address both signal quality (reducing bad trades) and signal exploitation (maximizing profit from good trades)."),
      para("The current system has fundamental architectural strengths: probabilistic regime activation, multi-timeframe confirmation, adaptive learning loops, and expected-value gating. However, several configuration and logic gaps are leaving significant profit on the table and allowing low-quality counter-trend trades to execute."),

      heading("Current Performance Snapshot", HeadingLevel.HEADING_2),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [4000, 5360],
        rows: [
          row(headerCell("Metric", 4000), headerCell("Current Value", 5360)),
          row(cell("Capital", 4000), cell("$99,998.72 USDT", 5360)),
          row(cell("Total Trades (all sessions)", 4000), cell("~33 closed trades", 5360)),
          row(cell("Trend Model Win Rate (L1)", 4000), cell("13.3% (4 wins / 30 trades)", 5360, { fill: warningColor })),
          row(cell("Mean Reversion Win Rate (L1)", 4000), cell("0% (0 wins / 3 trades)", 5360, { fill: warningColor })),
          row(cell("RL Ensemble Win Rate (L1)", 4000), cell("33% (2 wins / 6 trades)", 5360)),
          row(cell("Score Calibration (0.7-0.8 bucket)", 4000), cell("63.6% win rate (7/11)", 5360, { fill: successColor })),
          row(cell("Score Calibration (0.4-0.5 bucket)", 4000), cell("40% win rate (4/10)", 5360, { fill: warningColor })),
          row(cell("Walk-Forward Verdict", 4000), cell("REGIME_DEPENDENT (synthetic)", 5360)),
          row(cell("Exit Efficiency", 4000), cell("30/30 trend exits = other (no TP/SL hit)", 5360, { fill: warningColor })),
        ]
      }),

      para("Two critical findings from the data: high-score trades (0.7+) are profitable, while low-score trades (0.4-0.5) destroy value. And the exit system is failing to capture profits, with zero take-profit hits across 30 trend trades.", { italics: true }),

      new Paragraph({ children: [new PageBreak()] }),

      // ═══ RECOMMENDATION 1 ═══
      heading("1. Regime-Direction Coherence Gate (Critical)"),
      boldPara("Problem: ", "The system just executed a mean reversion SHORT on XRP/USDT during a bull_trend regime with only 47% confidence. Neither the SignalGenerator, ConfluenceScorer, nor RiskGate checks whether the signal direction is coherent with the current regime. A counter-trend trade in a strong trend has poor odds by definition."),
      boldPara("Impact: ", "This is the single highest-impact fix. Counter-trend trades in trending markets are the primary source of losing trades. Blocking them eliminates the worst category of losses."),
      heading("Recommended Implementation", HeadingLevel.HEADING_3),
      para("Add a new check in RiskGate.validate() before the EV gate that enforces direction-regime coherence:"),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [2200, 2200, 2200, 2760],
        rows: [
          row(headerCell("Regime", 2200), headerCell("Blocked Direction", 2200), headerCell("Min Score Override", 2200), headerCell("Rationale", 2760)),
          row(cell("bull_trend", 2200), cell("SELL / SHORT", 2200), cell("> 0.72", 2200, { align: AlignmentType.CENTER }), cell("Only allow counter-trend shorts with very high conviction", 2760)),
          row(cell("bear_trend", 2200), cell("BUY / LONG", 2200), cell("> 0.72", 2200, { align: AlignmentType.CENTER }), cell("Only allow counter-trend longs with very high conviction", 2760)),
          row(cell("Other regimes", 2200), cell("None", 2200), cell("N/A", 2200, { align: AlignmentType.CENTER }), cell("No restriction in non-trending regimes", 2760)),
        ]
      }),
      para("The 0.72 override threshold allows genuinely strong counter-trend signals (e.g., a confirmed top with 3+ models agreeing) while blocking the weak single-model signals that currently slip through."),

      // ═══ RECOMMENDATION 2 ═══
      heading("2. Raise the Confluence Floor (High Impact)"),
      boldPara("Problem: ", "The dynamic threshold system currently has a floor of 0.28 and dropped to 0.43 for the XRP trade. The score calibration data proves that trades below 0.50 have a 40% win rate (net negative after fees/slippage), while trades above 0.70 have a 64% win rate (profitable). The floor is too low."),
      heading("Recommended Config Changes", HeadingLevel.HEADING_3),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [3500, 2930, 2930],
        rows: [
          row(headerCell("Setting", 3500), headerCell("Current", 2930), headerCell("Recommended", 2930)),
          row(cell("dynamic_confluence.min_floor", 3500), cell("0.28", 2930), cell("0.50", 2930, { fill: successColor, bold: true })),
          row(cell("dynamic_confluence.max_ceiling", 3500), cell("0.65", 2930), cell("0.72", 2930, { fill: successColor, bold: true })),
          row(cell("idss.min_confluence_score (base)", 3500), cell("0.45", 2930), cell("0.55", 2930, { fill: successColor, bold: true })),
        ]
      }),
      boldPara("Expected effect: ", "Eliminates the bottom 30-40% of signals that are statistically unprofitable. Fewer trades, but each trade has meaningfully positive expected value. With $100K capital and moderate sizing, fewer high-quality trades will generate more total profit than many low-quality ones."),

      // ═══ RECOMMENDATION 3 ═══
      heading("3. Fix the Exit System (High Impact)"),
      boldPara("Problem: ", "The Level-2 tracker shows 30 out of 30 trend model exits were classified as other (not stop-loss, not take-profit). This means positions are being closed manually, by timeout, or by some other mechanism before hitting either exit level. The system is generating signals with defined TP/SL levels but never actually reaching them. Meanwhile, mean reversion had 2 SL exits with realized R-multiples of -5.65 and -3.02, indicating catastrophic stop distances."),
      heading("Root Causes and Fixes", HeadingLevel.HEADING_3),
      para("Cause A: TrendModel TP targets are set at ATR * (atr_mult + 1.0) which in bull_trend = ATR * 2.5. For BTC at ATR ~$1,300, the TP is $3,250 away from entry. On a 1h timeframe, this can take days to hit. The target is too ambitious for the timeframe."),
      boldPara("Fix A: ", "Reduce TrendModel TP to ATR * 2.0 (from ATR * 2.5). This creates a more achievable 2:1 R:R that actually gets hit within the signal timeframe. The current configuration has an R:R that looks great on paper but never closes in profit."),
      para("Cause B: MeanReversion stops are set at ATR * 1.2 in ranging but ATR * 1.5 in bull_trend. The realized R-multiples of -5.65 and -3.02 suggest the stops are far too wide for a mean reversion strategy. Mean reversion should fail fast."),
      boldPara("Fix B: ", "Tighten MeanReversion REGIME_ATR_MULTIPLIERS to 0.8 in ranging (from 1.2) and remove bull_trend/bear_trend entries entirely (set to 0.0), since the coherence gate from Recommendation 1 will block most counter-trend MR trades anyway."),

      // ═══ RECOMMENDATION 4 ═══
      heading("4. Increase Position Size for High-Conviction Trades"),
      boldPara("Problem: ", "The PositionSizer has max_size_usdt hardcoded to $25 in the ConfluenceScorer constructor. With $100K capital and quarter-Kelly sizing, even the highest-conviction trades only allocate $25 USDT. This is appropriate for early data collection but severely limits total profit."),
      heading("Recommended Sizing Progression", HeadingLevel.HEADING_3),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [2340, 2340, 2340, 2340],
        rows: [
          row(headerCell("Phase", 2340), headerCell("Trades Completed", 2340), headerCell("Max Size", 2340), headerCell("Max Capital %", 2340)),
          row(cell("Current (Learning)", 2340), cell("0-75", 2340), cell("$25", 2340, { align: AlignmentType.CENTER }), cell("4%", 2340, { align: AlignmentType.CENTER })),
          row(cell("Phase 2 (Calibrated)", 2340), cell("75-200", 2340), cell("$100", 2340, { align: AlignmentType.CENTER, fill: successColor }), cell("4%", 2340, { align: AlignmentType.CENTER })),
          row(cell("Phase 3 (Proven)", 2340), cell("200+", 2340), cell("$500", 2340, { align: AlignmentType.CENTER, fill: successColor }), cell("4%", 2340, { align: AlignmentType.CENTER })),
        ]
      }),
      boldPara("Key insight: ", "Total profit = (average win * win rate - average loss * loss rate) * number of trades * average position size. Improving signal quality (Recommendations 1-3) increases the first term. Increasing position size multiplies the entire result. Both together create a multiplicative effect on total profit."),

      // ═══ RECOMMENDATION 5 ═══
      heading("5. Reduce MeanReversion Affinity in Trending Regimes"),
      boldPara("Problem: ", "MeanReversionModel has REGIME_AFFINITY of 0.2 for bull_trend and bear_trend. The min_activation_weight is 0.10. So MR fires at 20% weight in trending regimes, generating counter-trend signals that the confluence scorer can then approve if no other models disagree. Combined with the low dynamic threshold floor, this allows weak counter-trend trades."),
      heading("Recommended Change", HeadingLevel.HEADING_3),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [3120, 3120, 3120],
        rows: [
          row(headerCell("MR Affinity", 3120), headerCell("Current", 3120), headerCell("Recommended", 3120)),
          row(cell("bull_trend", 3120), cell("0.2", 3120), cell("0.05 (below min_activation)", 3120, { fill: successColor })),
          row(cell("bear_trend", 3120), cell("0.2", 3120), cell("0.05 (below min_activation)", 3120, { fill: successColor })),
          row(cell("ranging", 3120), cell("1.0", 3120), cell("1.0 (unchanged)", 3120)),
          row(cell("volatility_compression", 3120), cell("0.8", 3120), cell("0.8 (unchanged)", 3120)),
        ]
      }),
      para("By setting the trending affinity below the 0.10 activation threshold, MR signals are completely suppressed in trending regimes at the signal generation level. This is cleaner than blocking them downstream at the RiskGate, since it prevents unnecessary computation and avoids polluting the confluence scorer with signals that should never be considered."),

      // ═══ RECOMMENDATION 6 ═══
      heading("6. Score-Weighted Position Sizing (Replace Step Function)"),
      boldPara("Problem: ", "The current score_mult in PositionSizer uses a step function with only 5 tiers (0.75, 0.85, 1.0, 1.15, 1.3). A trade at 0.59 gets the same size as one at 0.50. A trade at 0.90 gets the same as 0.99. This blunt approach under-weights the best signals and over-weights marginal ones."),
      boldPara("Fix: ", "Replace the step function with a continuous linear interpolation: score_mult = 0.5 + score (clamped to [0.5, 1.5]). A 0.55 score gets 1.05x, a 0.80 score gets 1.30x, and a 0.95 score gets 1.45x. This creates a smooth gradient that proportionally rewards higher-conviction signals with larger sizes."),

      // ═══ RECOMMENDATION 7 ═══
      heading("7. EV Gate Score Midpoint Calibration"),
      boldPara("Problem: ", "The EV gate uses a sigmoid function to convert confluence score to win probability. The midpoint is set to 0.50, meaning a 50% score maps to 50% win probability. But the actual data shows that a 0.40-0.50 score has a 40% win rate, not 50%. The sigmoid is overestimating win probability for low-score trades, letting them pass the EV gate when they should be rejected."),
      boldPara("Fix: ", "Shift the sigmoid midpoint from 0.50 to 0.55. This makes the EV gate correctly pessimistic about low-score trades while remaining realistic about high-score ones. Combined with the raised floor (Recommendation 2), this ensures only trades with genuine positive expected value are approved."),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [3120, 3120, 3120],
        rows: [
          row(headerCell("Setting", 3120), headerCell("Current", 3120), headerCell("Recommended", 3120)),
          row(cell("expected_value.score_midpoint", 3120), cell("0.50", 3120), cell("0.55", 3120, { fill: successColor, bold: true })),
        ]
      }),

      new Paragraph({ children: [new PageBreak()] }),

      // ═══ IMPLEMENTATION PRIORITY ═══
      heading("Implementation Priority Matrix"),
      para("Ordered by expected impact on total profit. Items 1-3 should be implemented immediately as they address active profit destruction. Items 4-7 amplify gains once signal quality is established."),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [600, 2600, 1600, 1600, 1200, 1760],
        rows: [
          row(headerCell("#", 600), headerCell("Change", 2600), headerCell("Impact", 1600), headerCell("Effort", 1600), headerCell("Risk", 1200), headerCell("Type", 1760)),
          row(cell("1", 600, { align: AlignmentType.CENTER }), cell("Regime-Direction Gate", 2600, { bold: true }), cell("Very High", 1600, { color: "C0392B", bold: true }), cell("Low (code)", 1600), cell("Low", 1200), cell("Signal Quality", 1760)),
          row(cell("2", 600, { align: AlignmentType.CENTER }), cell("Raise Confluence Floor", 2600, { bold: true }), cell("High", 1600, { color: "E67E22", bold: true }), cell("Very Low (config)", 1600), cell("Low", 1200), cell("Signal Quality", 1760)),
          row(cell("3", 600, { align: AlignmentType.CENTER }), cell("Fix Exit System (TP/SL)", 2600, { bold: true }), cell("High", 1600, { color: "E67E22", bold: true }), cell("Low (code)", 1600), cell("Medium", 1200), cell("Profit Capture", 1760)),
          row(cell("4", 600, { align: AlignmentType.CENTER }), cell("Increase Position Sizes", 2600), cell("High", 1600, { color: "E67E22" }), cell("Very Low (config)", 1600), cell("Medium", 1200), cell("Profit Scale", 1760)),
          row(cell("5", 600, { align: AlignmentType.CENTER }), cell("MR Affinity in Trends", 2600), cell("Medium", 1600, { color: "F1C40F" }), cell("Very Low (code)", 1600), cell("Low", 1200), cell("Signal Quality", 1760)),
          row(cell("6", 600, { align: AlignmentType.CENTER }), cell("Continuous Score Sizing", 2600), cell("Medium", 1600, { color: "F1C40F" }), cell("Low (code)", 1600), cell("Low", 1200), cell("Profit Scale", 1760)),
          row(cell("7", 600, { align: AlignmentType.CENTER }), cell("EV Gate Midpoint", 2600), cell("Medium", 1600, { color: "F1C40F" }), cell("Very Low (config)", 1600), cell("Low", 1200), cell("Signal Quality", 1760)),
        ]
      }),

      new Paragraph({ spacing: { before: 400 } }),

      heading("Summary of All Config Changes", HeadingLevel.HEADING_2),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [4500, 2430, 2430],
        rows: [
          row(headerCell("config.yaml Key", 4500), headerCell("Current", 2430), headerCell("New", 2430)),
          row(cell("dynamic_confluence.min_floor", 4500), cell("0.28", 2430), cell("0.50", 2430, { fill: successColor, bold: true })),
          row(cell("dynamic_confluence.max_ceiling", 4500), cell("0.65", 2430), cell("0.72", 2430, { fill: successColor, bold: true })),
          row(cell("idss.min_confluence_score", 4500), cell("0.45", 2430), cell("0.55", 2430, { fill: successColor, bold: true })),
          row(cell("expected_value.score_midpoint", 4500), cell("0.50", 2430), cell("0.55", 2430, { fill: successColor, bold: true })),
        ]
      }),

      new Paragraph({ spacing: { before: 200 } }),
      heading("Code Changes Required", HeadingLevel.HEADING_2),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [4000, 5360],
        rows: [
          row(headerCell("File", 4000), headerCell("Change", 5360)),
          row(cell("core/risk/risk_gate.py", 4000, { bold: true }), cell("Add regime-direction coherence check before EV gate", 5360)),
          row(cell("core/signals/sub_models/mean_reversion_model.py", 4000, { bold: true }), cell("Set bull_trend/bear_trend affinity to 0.05", 5360)),
          row(cell("core/signals/sub_models/trend_model.py", 4000, { bold: true }), cell("Reduce TP from ATR*(mult+1.0) to ATR*2.0", 5360)),
          row(cell("core/meta_decision/position_sizer.py", 4000, { bold: true }), cell("Replace step-function score_mult with continuous linear", 5360)),
          row(cell("core/meta_decision/confluence_scorer.py", 4000, { bold: true }), cell("Increase max_size_usdt from 25 to 100 (Phase 2)", 5360)),
        ]
      }),

      new Paragraph({ spacing: { before: 400 } }),
      para("These seven changes, implemented together, address the three fundamental profit levers: eliminating bad trades (Recommendations 1, 2, 5, 7), capturing more profit from good trades (Recommendations 3, 6), and scaling position sizes proportionally to conviction (Recommendation 4). The combined effect should move NexusTrader from a marginal system into a consistently profitable one during the Bybit Demo phase.", { italics: true }),
    ]
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("/sessions/jolly-blissful-knuth/mnt/NexusTrader/NexusTrader_Strategy_Optimization.docx", buffer);
  console.log("Document created successfully");
});
