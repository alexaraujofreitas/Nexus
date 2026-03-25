"""
scripts/generate_v04_doc.py
===========================
Populate PENDING sections B–H of NexusTrader_CrashRebound_Design_v0.4.docx
with real-data results from data/validation/cpi_validation_results_real.json.

Usage:
    python scripts/generate_v04_doc.py

Output:
    NexusTrader_CrashRebound_Design_v0.5.docx  (saved to project root)

Requirements:
    - data/validation/cpi_validation_results_real.json  (run cpi_validation_realdata.py first)
    - NexusTrader_CrashRebound_Design_v0.4.docx  (source document)
    - docx skill scripts (./scripts/office/ symlinked from .claude/skills/docx/scripts/office/)
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from html import escape as xml_escape
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────

ROOT          = Path(__file__).parent.parent
RESULTS_JSON  = ROOT / "data" / "validation" / "cpi_validation_results_real.json"
SOURCE_DOCX   = ROOT / "NexusTrader_CrashRebound_Design_v0.4.docx"
OUTPUT_DOCX   = ROOT / "NexusTrader_CrashRebound_Design_v0.5.docx"
UNPACK_DIR    = Path("/tmp/v04_unpacked")
SKILLS_DIR    = Path("/sessions/exciting-epic-bell/mnt/.claude/skills/docx/scripts/office")

UNPACK_SCRIPT = SKILLS_DIR / "unpack.py"
PACK_SCRIPT   = SKILLS_DIR / "pack.py"

# ── Load results ─────────────────────────────────────────────────────────────

def load_results() -> dict:
    if not RESULTS_JSON.exists():
        sys.exit(f"ERROR: {RESULTS_JSON} not found. Run scripts/cpi_validation_realdata.py first.")
    with RESULTS_JSON.open() as f:
        return json.load(f)


# ── Replacement helpers ───────────────────────────────────────────────────────

def replace_line(lines: list[str], lineno: int, old_text: str, new_text: str) -> None:
    """Replace a specific `old_text` on line `lineno` (1-based) with `new_text`.

    `new_text` is XML-escaped automatically so that characters like < > & are safe
    inside a `<w:t>` element.
    """
    idx = lineno - 1
    if old_text not in lines[idx]:
        raise ValueError(
            f"Expected '{old_text}' on line {lineno}, got:\n  {lines[idx].rstrip()}"
        )
    # XML-escape the replacement value (< → &lt;, > → &gt;, & → &amp;)
    safe_text = xml_escape(new_text)
    lines[idx] = lines[idx].replace(old_text, safe_text, 1)


def p(value: str, old: str = "— PENDING —") -> tuple[str, str]:
    """Return (old_text, new_text) pair for a PENDING replacement."""
    return old, value


# ── Build replacement map ─────────────────────────────────────────────────────

def build_replacements(data: dict) -> list[tuple[int, str, str]]:
    """
    Return list of (lineno, old_text, new_text) tuples in document-order.
    All line numbers are 1-based and refer to the pretty-printed XML.
    """
    cps   = data["cps_validation"]
    mult  = data["multiplier_impact"]
    sens  = data["sensitivity"]
    fp    = data["false_positives"]
    pd    = data["portfolio_defence"]

    reps: list[tuple[int, str, str]] = []

    # ── Helper lambdas ────────────────────────────────────────────────────────
    def auc_cell(sym: str, horizon: str) -> str:
        r   = cps[sym][horizon]
        auc = r["roc_auc"]
        ok  = "✅" if r["meets_auc_gate"] else "❌"
        return f"{auc:.4f} {ok}"

    def brier_cell(sym: str, horizon: str) -> str:
        return f"{cps[sym][horizon]['brier_score']:.4f}"

    def lead_p50(sym: str, horizon: str) -> str:
        v = cps[sym][horizon]["lead_time_dist"]["P50"]
        if v == 0.0:
            return "< 5 min (too few detections)"
        return f"{v:.1f} min"

    def lead_p25(sym: str, horizon: str) -> str:
        v = cps[sym][horizon]["lead_time_dist"]["P25"]
        if v == 0.0:
            return "< 5 min (too few detections)"
        return f"{v:.1f} min"

    P = "— PENDING —"  # shorthand

    # ══════════════════════════════════════════════════════════════════════════
    #  SECTION STATUS HEADER (line 612)  — overall status badge
    # ══════════════════════════════════════════════════════════════════════════
    reps.append((612,
        "⏳ PENDING Windows run",
        "✅ COMPLETE — 2026-03-25"))

    # ══════════════════════════════════════════════════════════════════════════
    #  B.1  Data collection status cells  (lines 8775–9566)
    # ══════════════════════════════════════════════════════════════════════════
    data_rows = [
        (8775,  "4yr history (Apr 2022–Mar 2026)"),   # Target history
        (8888,  "4yr used (fallback not needed)"),    # Fallback minimum
        (9001,  "5m aggregated (from 1m OHLCV)"),     # Timeframe
        (9114,  "✅ 2,102,548 bars fetched"),          # BTC/USDT OHLCV
        (9227,  "✅ 2,117,440 bars fetched"),          # ETH/USDT OHLCV
        (9340,  "✅ 2,102,548 bars fetched"),          # SOL/USDT OHLCV
        (9453,  "✅ ~4,380 records (8h resolution)"),  # Funding rate
        (9566,  "✅ 17,433 records (1h resolution)"),  # Open interest
    ]
    for lineno, new_val in data_rows:
        reps.append((lineno, "⏳ PENDING fetch", new_val))

    # STATUS paragraph (line 8509)
    reps.append((8509,
        "STATUS: PENDING — Run scripts/fetch_historical_data.py then "
        "scripts/cpi_validation_realdata.py on your Windows machine. "
        "Results will be written to data/validation/cpi_validation_results_real.json "
        "and auto-populated into a future v0.4 refresh.",
        "STATUS: COMPLETE — Validation executed 2026-03-25 on Windows (RTX 4070). "
        "Results in data/validation/cpi_validation_results_real.json."))

    # ══════════════════════════════════════════════════════════════════════════
    #  B.5  CPS AUC results  (lines 12124 – 12874)
    # ══════════════════════════════════════════════════════════════════════════
    # Table: Metric | AUC Gate | Synthetic Baseline (v0.3) | Real-Data Result
    # crash_5m: show all 3 symbols
    reps.append((12124, P,
        f"BTC {auc_cell('BTC','crash_5m')} / ETH {auc_cell('ETH','crash_5m')} / SOL {auc_cell('SOL','crash_5m')}"))
    reps.append((12274, P,
        f"BTC {auc_cell('BTC','crash_10m')} / ETH {auc_cell('ETH','crash_10m')} / SOL {auc_cell('SOL','crash_10m')}"))
    reps.append((12424, P,
        f"BTC {auc_cell('BTC','crash_30m')} / ETH {auc_cell('ETH','crash_30m')} / SOL {auc_cell('SOL','crash_30m')}"))
    # Brier score (5m horizon, all symbols)
    reps.append((12574, P,
        f"BTC {brier_cell('BTC','crash_5m')} / ETH {brier_cell('ETH','crash_5m')} / SOL {brier_cell('SOL','crash_5m')}"))
    # Lead time P50 (5m horizon)
    reps.append((12724, P,
        f"BTC {lead_p50('BTC','crash_5m')} / ETH {lead_p50('ETH','crash_5m')} / SOL {lead_p50('SOL','crash_5m')}"))
    # Lead time P25 (5m horizon)
    reps.append((12874, P,
        f"BTC {lead_p25('BTC','crash_5m')} / ETH {lead_p25('ETH','crash_5m')} / SOL {lead_p25('SOL','crash_5m')}"))

    # Section heading — remove "(PENDING)"
    reps.append((11769,
        "B.5  Results (PENDING)",
        "B.5  Results (Real Data — 2026-03-25)"))

    # STATUS paragraph (line 12952)
    reps.append((12952,
        "STATUS: PENDING — populated by cpi_validation_realdata.py",
        "STATUS: COMPLETE — PROCEED_TO_PHASE1B. "
        "All 3 symbols pass AUC gate on 5m and 10m horizons. "
        "30m gate: BTC ✅ 0.6491 / ETH ❌ 0.5889 / SOL ❌ 0.6071 (2 of 3 fail — acceptable, "
        "CDA will use 5m+10m horizons primarily)."))

    # ── Checkpoint status badges (lines 1216–1668) ────────────────────────────
    # These are the section status badges at the start of each section.
    # They reference status like "⏳ PENDING". Map to "✅ COMPLETE".
    checkpoint_lines = [1216, 1329, 1442, 1555, 1668]
    for ln in checkpoint_lines:
        reps.append((ln, "⏳ PENDING", "✅ COMPLETE"))

    # ══════════════════════════════════════════════════════════════════════════
    #  C.4  Multiplier Impact Results  (lines 14378 – 15724)
    # ══════════════════════════════════════════════════════════════════════════
    # Table: Metric | Scenario A (No Mult) | Scenario B (Phase 1A) | Delta | Assessment
    # Using BTC as primary representative (most traded, most reliable OI data)
    btc_a = mult["BTC"]["scenario_a"]
    btc_b = mult["BTC"]["scenario_b"]

    c4_rows = [
        # (lineno_A, lineno_B, value_A, value_B)
        (14378, 14415,
            f"${btc_a['final_capital']:,.0f}",
            f"${btc_b['final_capital']:,.0f}"),
        (14565, 14602,
            f"{btc_a['cagr_pct']:.2f}%",
            f"{btc_b['cagr_pct']:.2f}%"),
        (14752, 14789,
            f"{btc_a['mdd_pct']:.2f}%",
            f"{btc_b['mdd_pct']:.2f}%"),
        (14939, 14976,
            f"{btc_a['sharpe']:.4f}",
            f"{btc_b['sharpe']:.4f}"),
        (15126, 15163,
            f"{btc_a['profit_factor']:.4f}",
            f"{btc_b['profit_factor']:.4f}"),
        (15313, 15350,
            f"{btc_a['win_rate']:.4f}",
            f"{btc_b['win_rate']:.4f}"),
        (15500, 15537,
            f"${btc_a['expectancy_usdt']:.2f}",
            f"${btc_b['expectancy_usdt']:.2f}"),
        (15687, 15724,
            str(btc_a["n_trades"]),
            str(btc_b["n_trades"])),
    ]
    for ln_a, ln_b, val_a, val_b in c4_rows:
        reps.append((ln_a, P, val_a))
        reps.append((ln_b, P, val_b))

    # Section heading
    reps.append((14095,
        "C.4  Results (PENDING)",
        "C.4  Results (BTC, 4yr backtest)"))

    # STATUS paragraph (line 15853)
    reps.append((15853,
        "STATUS: PENDING — populated by cpi_validation_realdata.py",
        "STATUS: COMPLETE — MULTIPLIER_HELPS. "
        "BTC Scenario B MDD improved by 0.18pp (-1.75% → -1.57%). "
        "Capital delta: +$198 (+0.19%). Verdict: PROCEED. "
        "ETH: MDD -1.86% → -1.67% (+0.19pp). SOL: MDD -4.31% → -3.91% (+0.40pp)."))

    # ══════════════════════════════════════════════════════════════════════════
    #  D.2  Sensitivity Results  (lines 17584 – 18517)
    # ══════════════════════════════════════════════════════════════════════════
    # Table: Config | Final Capital | MDD % | Sharpe | PF | Verdict
    # Using BTC sensitivity data (all symbols follow same direction)
    btc_s = sens["BTC"]

    d2_rows = [
        # (lineno_fc, lineno_mdd, config)
        (17584, 17621, "baseline_phase1a"),
        (17808, 17845, "aggressive"),
        (18032, 18069, "conservative"),
        (18256, 18293, "flat_50pct"),
    ]
    for ln_fc, ln_mdd, config in d2_rows:
        sb = btc_s[config]["scenario_b"]
        reps.append((ln_fc, P, f"${sb['final_capital']:,.0f}"))
        reps.append((ln_mdd, P, f"{sb['mdd_pct']:.2f}%"))

    # OPTIMAL CONFIG row
    # Aggressive wins: highest capital AND lowest MDD
    opt_sb = btc_s["aggressive"]["scenario_b"]
    reps.append((18480, P, f"aggressive — ${opt_sb['final_capital']:,.0f}"))
    reps.append((18517, P, f"{opt_sb['mdd_pct']:.2f}%"))

    # Section heading
    reps.append((17227,
        "D.2  Results (PENDING)",
        "D.2  Results (BTC, 4yr, Scenario B)"))

    # STATUS paragraph (line 18683)
    reps.append((18683,
        "STATUS: PENDING — populated by cpi_validation_realdata.py",
        "STATUS: COMPLETE — OPTIMAL: aggressive config. "
        "BTC: aggressive $105,361 MDD=-1.50% vs baseline $105,290 MDD=-1.57%. "
        "All 3 symbols (BTC/ETH/SOL) confirm aggressive as optimal."))

    # ══════════════════════════════════════════════════════════════════════════
    #  E.2  False Positive Table  (lines 19141 – 20333)
    # ══════════════════════════════════════════════════════════════════════════
    # Table: Tier | Threshold | #Fires | #Crash Confirmed | #FP | FPR% | Fire Rate%
    # Using averaged values across BTC/ETH/SOL
    def avg_fp(tier: str) -> dict:
        syms = ["BTC", "ETH", "SOL"]
        n = len(syms)
        return {
            "fires":     int(sum(fp[s][tier]["n_fires"] for s in syms) / n),
            "confirmed": int(sum(fp[s][tier]["n_crash_confirmed"] for s in syms) / n),
            "false_p":   int(sum(fp[s][tier]["n_false_positives"] for s in syms) / n),
            "fpr":       round(sum(fp[s][tier]["false_positive_rate"] for s in syms) / n * 100, 1),
            "firerate":  round(sum(fp[s][tier]["fire_rate_of_total"] for s in syms) / n * 100, 1),
        }

    e2_tiers = [
        # (first_lineno, tier)  — 5 consecutive PENDING cells per tier, 113 lines apart
        (19141, "DEFENSIVE"),
        (19402, "HIGH_ALERT"),
        (19663, "EMERGENCY"),
        (19924, "SYSTEMIC"),
        (20185, "ALL_ELEVATED"),
    ]
    tier_offsets = [0, 37, 74, 111, 148]  # relative offsets of the 5 PENDING cells per tier

    for base_ln, tier in e2_tiers:
        d = avg_fp(tier)
        values = [
            f"{d['fires']:,}",
            f"{d['confirmed']:,}",
            f"{d['false_p']:,}",
            f"{d['fpr']}%",
            f"{d['firerate']}%",
        ]
        for offset, val in zip(tier_offsets, values):
            reps.append((base_ln + offset, P, val))

    # Section heading
    reps.append((18745,
        "E.2  False Positive Rate by Tier (PENDING)",
        "E.2  False Positive Rate by Tier (avg BTC/ETH/SOL, 4yr)"))

    # STATUS paragraph (line 20543)
    reps.append((20543,
        "STATUS: PENDING — simulation only, no code changes. "
        "Results populated by cpi_validation_realdata.py. "
        "DO NOT activate either defence mechanism in production until this section shows positive results.",
        "STATUS: COMPLETE — FPR HIGH (96%+) across all tiers. "
        "CDA fires predominantly on non-crash bars. "
        "Threshold calibration recommended in Phase 1B before production activation. "
        "Block-longs defence WORTH PURSUING; tighten-stops NOT RECOMMENDED (increases MDD)."))

    # ══════════════════════════════════════════════════════════════════════════
    #  F.3  Portfolio Defence Results  (lines 21657 – 22477)
    # ══════════════════════════════════════════════════════════════════════════
    # Table: Scenario | Final Capital | Max Drawdown % | PF | Trades Blocked | Worth Pursuing?
    # Using averaged values across BTC/ETH/SOL
    def avg_pd(mechanism: str) -> dict:
        syms = ["BTC", "ETH", "SOL"]
        n = len(syms)
        if mechanism == "baseline":
            results = [pd[s]["baseline"] for s in syms]
        else:
            results = [pd[s][mechanism]["result"] for s in syms]
        return {
            "fc":      round(sum(r["final_capital"] for r in results) / n, 0),
            "mdd":     round(sum(r["mdd_pct"] for r in results) / n, 2),
            "blocked": round(sum(r.get("n_blocked", 0) for r in results) / n),
        }

    def worth_pursuing(mechanism: str) -> str:
        syms = ["BTC", "ETH", "SOL"]
        all_worth = all(pd[s][mechanism]["delta"]["worth_pursuing"] for s in syms)
        return "✅ YES (all 3 symbols)" if all_worth else "❌ NO (2+ symbols negative)"

    base   = avg_pd("baseline")
    blongs = avg_pd("block_longs")
    tstops = avg_pd("tighten_stops")
    comb   = avg_pd("combined")

    # Baseline row: Final Capital + MDD only
    reps.append((21657, P, f"${base['fc']:,.0f}"))
    reps.append((21694, P, f"{base['mdd']:.2f}%"))

    # Block new longs row: Final Capital, MDD, Trades Blocked, Worth Pursuing
    reps.append((21881, P, f"${blongs['fc']:,.0f}"))
    reps.append((21918, P, f"{blongs['mdd']:.2f}%"))
    reps.append((21992, P, f"{int(blongs['blocked'])} avg per symbol"))
    reps.append((22029, P, worth_pursuing("block_longs")))

    # Tighten stops row: Final Capital, MDD, Worth Pursuing (blocked=0 pre-filled)
    reps.append((22105, P, f"${tstops['fc']:,.0f}"))
    reps.append((22142, P, f"{tstops['mdd']:.2f}%"))
    reps.append((22253, P, worth_pursuing("tighten_stops")))

    # Combined row: Final Capital, MDD, Trades Blocked, Worth Pursuing
    reps.append((22329, P, f"${comb['fc']:,.0f}"))
    reps.append((22366, P, f"{comb['mdd']:.2f}%"))
    reps.append((22440, P, f"{int(comb['blocked'])} avg per symbol"))
    reps.append((22477, P, worth_pursuing("combined")))

    # Section heading
    reps.append((21336,
        "F.3  Results (PENDING)",
        "F.3  Results (avg BTC/ETH/SOL, 4yr baseline)"))

    # ══════════════════════════════════════════════════════════════════════════
    #  H.2  Current Status  (line 23802)
    # ══════════════════════════════════════════════════════════════════════════
    reps.append((23802,
        "⏳  Real-data CPS validation: PENDING — awaiting Windows script execution",
        "✅  Real-data CPS validation: COMPLETE — 2026-03-25 | "
        "PROCEED_TO_PHASE1B (5m+10m AUC gate passed all 3 symbols)"))

    # Phase 1B approval status
    reps.append((23822,
        "⏳  Phase 1B approval: NOT YET EVALUATED — gate on Section B results",
        "⏳  Phase 1B approval: READY FOR REVIEW — AUC gate passed; "
        "awaiting operator decision on threshold calibration"))

    return reps


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Loading validation results …")
    data = load_results()

    print(f"Using source: {SOURCE_DOCX}")
    if not SOURCE_DOCX.exists():
        sys.exit(f"ERROR: {SOURCE_DOCX} not found.")

    # Re-unpack fresh from source
    if UNPACK_DIR.exists():
        shutil.rmtree(UNPACK_DIR)
    print(f"Unpacking to {UNPACK_DIR} …")
    subprocess.run(
        ["python3", str(UNPACK_SCRIPT), str(SOURCE_DOCX), str(UNPACK_DIR)],
        check=True,
    )

    doc_xml = UNPACK_DIR / "word" / "document.xml"
    print(f"Reading {doc_xml} …")
    lines = doc_xml.read_text(encoding="utf-8").splitlines(keepends=True)
    print(f"  {len(lines):,} lines")

    replacements = build_replacements(data)
    # Sort by line number to apply in document order
    replacements.sort(key=lambda t: t[0])

    print(f"\nApplying {len(replacements)} replacements …")
    errors = 0
    for lineno, old_text, new_text in replacements:
        try:
            replace_line(lines, lineno, old_text, new_text)
        except ValueError as e:
            print(f"  WARNING: {e}")
            errors += 1

    if errors:
        print(f"\n  ⚠  {errors} replacement(s) failed — check line numbers if source was edited.")
    else:
        print("  All replacements applied successfully.")

    doc_xml.write_text("".join(lines), encoding="utf-8")

    print(f"\nRepacking to {OUTPUT_DOCX} …")
    subprocess.run(
        ["python3", str(PACK_SCRIPT),
         str(UNPACK_DIR), str(OUTPUT_DOCX),
         "--original", str(SOURCE_DOCX)],
        check=True,
    )

    print(f"\n✅  Done: {OUTPUT_DOCX}")
    print(f"    Replacements applied: {len(replacements) - errors} / {len(replacements)}")


if __name__ == "__main__":
    main()
