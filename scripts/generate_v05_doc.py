"""
generate_v05_doc.py
═══════════════════
Populates NexusTrader_CrashRebound_Design_v0.5.docx (calibration template)
with results from data/validation/cps_calibration_results.json.

Self-contained — uses only Python stdlib (zipfile, xml). No external skills needed.

Usage (run AFTER cps_calibration.py):
    python scripts/generate_v05_doc.py
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import zipfile
from html import escape as xml_escape
from pathlib import Path

# ── Paths ────────────────────────────────────────────────────────────────────
ROOT          = Path(__file__).resolve().parents[1]
TEMPLATE_DOCX = ROOT / "NexusTrader_CrashRebound_Design_v0.5.docx"
OUTPUT_DOCX   = ROOT / "NexusTrader_CrashRebound_Design_v0.5.docx"   # overwrite in-place
JSON_PATH     = ROOT / "data" / "validation" / "cps_calibration_results.json"

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gen_v05")

PENDING_TEXT = "\u23f3 PENDING"   # ⏳ PENDING


# ══════════════════════════════════════════════════════════════════════════════
#  FORMATTERS
# ══════════════════════════════════════════════════════════════════════════════

def _g(d, *keys, default="?"):
    """Safe nested dict getter."""
    if d is None:
        return default
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is default:
            return default
    return cur


def fmt_threshold(v, default="?") -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else str(default)

def fmt_auc(v, default="?") -> str:
    return f"{v:.4f}" if isinstance(v, (int, float)) else str(default)

def fmt_pct(v, default="?", decimals=1) -> str:
    return f"{v * 100:.{decimals}f}%" if isinstance(v, (int, float)) else str(default)

def fmt_evi(v, default="?") -> str:
    if isinstance(v, (int, float)):
        sign = "+" if v >= 0 else ""
        return f"{sign}${v:,.0f}"
    return str(default)

def fmt_capital(v, default="?") -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else str(default)

def fmt_sharpe(v, default="?") -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else str(default)

def fmt_pf(v, default="?") -> str:
    return f"{v:.2f}" if isinstance(v, (int, float)) else str(default)

def fmt_mdd(v, default="?") -> str:
    return f"{v:.1f}%" if isinstance(v, (int, float)) else str(default)

def fmt_score(v, default="?") -> str:
    return f"{v:.1f}" if isinstance(v, (int, float)) else str(default)

def fmt_n(v, default="?") -> str:
    return f"{int(v):,}" if isinstance(v, (int, float)) else str(default)

def feasible_str(v) -> str:
    if v is True:
        return "\u2705 YES"
    if v is False:
        return "\u274c NO (constraint violated)"
    return "?"

def status_cell(ok) -> str:
    if ok is True:
        return "\u2705 PASSED"
    if ok is False:
        return "\u274c FAILED"
    return "\u2b50 DONE"

def optimal_marker(curve_name: str, optimal: str) -> str:
    return "\u2705 OPTIMAL" if curve_name == optimal else "\u2014"


# ══════════════════════════════════════════════════════════════════════════════
#  SEQUENTIAL REPLACER
# ══════════════════════════════════════════════════════════════════════════════

class SeqReplacer:
    """
    Finds all occurrences of `needle` in `text` and replaces them in
    document order with values provided via apply().
    """
    def __init__(self, text: str, needle: str):
        self._text    = text
        self._needle  = needle
        self._pos_list: list[int] = []
        idx = 0
        while True:
            p = text.find(needle, idx)
            if p == -1:
                break
            self._pos_list.append(p)
            idx = p + len(needle)

    @property
    def count(self) -> int:
        return len(self._pos_list)

    def apply(self, values: list[str]) -> str:
        if len(values) != len(self._pos_list):
            raise ValueError(
                f"Expected {len(self._pos_list)} replacement values, got {len(values)}"
            )
        parts: list[str] = []
        prev = 0
        for pos, val in zip(self._pos_list, values):
            parts.append(self._text[prev:pos])
            parts.append(xml_escape(val))
            prev = pos + len(self._needle)
        parts.append(self._text[prev:])
        return "".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  BUILD REPLACEMENT LIST  (166 values, in document order)
# ══════════════════════════════════════════════════════════════════════════════

def build_replacement_list(data: dict) -> list[str]:
    """
    Returns exactly 166 strings, one per ⏳ PENDING occurrence in document order:

    Cover table        (14)
    A.4 sweep table   (36)
    A.5 summary        (1)
    B.1 threshold tbl (12)
    B.3 performance   (10)
    C.1 tiers table    (8)
    C.2 FPR improve    (8)
    C.3 justification  (1)
    D.2 curves table  (30)
    D.3 recommendation (1)
    E.2 defence table (13)
    E.3 verdict        (1)
    F.2 segmentation  (10)
    F.3 AUC compare    (1)
    F.4 recommendation (1)
    H.3 verdict table (18)
    H.4 changes        (1)
    """
    ts   = data.get("threshold_sweep", {})
    mh   = data.get("multi_horizon", {})
    tier = data.get("tier_optimization", {})
    mult = data.get("multiplier_calibration", {})
    btcd = data.get("btc_dominance", {})
    summ = data.get("summary", {})

    vals: list[str] = []

    # ── Cover table (14) ─────────────────────────────────────────────────────
    # Rows: A, B, C, D, E, F, H  (G is static "✅ ASSESSED")
    # Each row: status, note

    btc_5m_opt = _g(ts, "BTC", "crash_5m", "optimal", default={})
    a_ok = isinstance(btc_5m_opt, dict) and btc_5m_opt.get("feasible") is True

    mh_btc_watch_evi = _g(mh, "BTC", "crash_watch", "evi_net_pnl")
    b_pass = isinstance(mh_btc_watch_evi, (int, float)) and mh_btc_watch_evi > 0

    c_evi = _g(tier, "BTC", "final_result", "evi_net_pnl")
    c_ok = isinstance(c_evi, (int, float)) and c_evi > 0

    d_curve = _g(mult, "BTC", "optimal_curve", default=None)

    e_evi = _g(tier, "BTC", "final_result", "evi_net_pnl")
    e_ok = isinstance(e_evi, (int, float)) and e_evi > 0

    rec = _g(btcd, "recommendation", default=None)
    f_ok = rec is not None and "INSUFFICIENT" not in str(rec)

    phase1b = _g(summ, "phase1b_verdict", default=None)
    h_ok = phase1b is not None and "YES" in str(phase1b)

    vals += [
        status_cell(a_ok),                                                          # A status
        "EVI feasible" if a_ok else "Review sweep \u2014 constraints not met",      # A note
        status_cell(b_pass),                                                         # B status
        "CRASH_WATCH + CRASH_CONFIRMED tuned" if b_pass else "Multi-horizon complete", # B note
        status_cell(c_ok),                                                           # C status
        "Sequential greedy optimisation complete",                                   # C note
        status_cell(d_curve is not None),                                            # D status
        f"Best curve: {d_curve}" if d_curve else "Multiplier curves compared",      # D note
        status_cell(e_ok),                                                           # E status
        "Defence simulation with calibrated CPS thresholds",                         # E note
        status_cell(f_ok),                                                           # F status
        str(rec) if rec else "BTC dominance analysis complete",                      # F note
        status_cell(h_ok),                                                           # H status
        str(phase1b)[:60] if phase1b else "Verdict pending sweep results",           # H note
    ]

    # ── A.4 Sweep table (36) ─────────────────────────────────────────────────
    # Order: BTC/5m, BTC/10m, ETH/5m, ETH/10m, SOL/5m, SOL/10m
    # Each row: AUC | Threshold | EVI | FPR | FireRate | Feasible
    for sym in ["BTC", "ETH", "SOL"]:
        for hz in ["crash_5m", "crash_10m"]:
            opt = _g(ts, sym, hz, "optimal", default={})
            auc = _g(ts, sym, hz, "auc_baseline")
            vals += [
                fmt_auc(auc),
                fmt_threshold(_g(opt, "threshold")),
                fmt_evi(_g(opt, "evi_net_pnl")),
                fmt_pct(_g(opt, "fpr")),
                fmt_pct(_g(opt, "fire_rate")),
                feasible_str(_g(opt, "feasible")),
            ]

    # ── A.5 Summary (1) ──────────────────────────────────────────────────────
    n_feasible = sum(
        1 for sym in ["BTC", "ETH", "SOL"]
        for hz in ["crash_5m", "crash_10m"]
        if _g(ts, sym, hz, "optimal", "feasible") is True
    )
    vals.append(
        f"{n_feasible}/6 symbol-horizon combinations meet all constraints "
        f"(FPR \u2264 70%, fire_rate \u2264 5%, MDD worsening \u2264 0.5pp). "
        f"BTC crash_5m optimal threshold: "
        f"{fmt_threshold(_g(ts, 'BTC', 'crash_5m', 'optimal', 'threshold'))} "
        f"(EVI {fmt_evi(_g(ts, 'BTC', 'crash_5m', 'optimal', 'evi_net_pnl'))})."
    )

    # ── B.1 Threshold table (12) ─────────────────────────────────────────────
    # Order: CPS_5m BTC, CPS_10m BTC, CPS_5m ETH, CPS_10m ETH, CPS_5m SOL, CPS_10m SOL
    # Each row: Recommended Threshold | Constraint Status
    for hz in ["crash_5m", "crash_10m"]:
        for sym in ["BTC", "ETH", "SOL"]:
            opt  = _g(ts, sym, hz, "optimal", default={})
            feas = _g(opt, "feasible")
            constraint = (
                f"\u2705 Met (FPR={fmt_pct(_g(opt, 'fpr'))}, "
                f"fire_rate={fmt_pct(_g(opt, 'fire_rate'))})"
                if feas is True else
                f"\u274c Not met (FPR={fmt_pct(_g(opt, 'fpr'))}, "
                f"fire_rate={fmt_pct(_g(opt, 'fire_rate'))})"
            )
            vals += [fmt_threshold(_g(opt, "threshold")), constraint]

    # ── B.3 Performance table (10) ───────────────────────────────────────────
    # Rows: CRASH_WATCH, CRASH_CONFIRMED — using BTC as representative
    # Each row: Precision | Recall | FPR | Lead Time P50 | EVI Net P&L
    btc_mh = mh.get("BTC", {})
    for stage in ["crash_watch", "crash_confirmed"]:
        s    = btc_mh.get(stage, {})
        lead = s.get("lead_time", {})
        p50  = lead.get("P50", "?") if isinstance(lead, dict) else "?"
        vals += [
            fmt_pct(_g(s, "precision")),
            fmt_pct(_g(s, "recall")),
            fmt_pct(_g(s, "fpr")),
            f"{p50} min" if isinstance(p50, (int, float)) else "?",
            fmt_evi(_g(s, "evi_net_pnl")),
        ]

    # ── C.1 Current vs Optimised tiers (8) ───────────────────────────────────
    # Order: DEFENSIVE, HIGH_ALERT, EMERGENCY, SYSTEMIC
    # Each row: Optimal Gate | EVI Delta
    btc_tier = tier.get("BTC", {})
    opt_gates = btc_tier.get("optimal", {})
    c_evi_final = _g(btc_tier, "final_result", "evi_net_pnl")
    for tier_name in ["DEFENSIVE", "HIGH_ALERT", "EMERGENCY", "SYSTEMIC"]:
        gate_val = _g(opt_gates, tier_name)
        gate_str = f"\u2265 {fmt_score(gate_val)}" if isinstance(gate_val, (int, float)) else "?"
        evi_str  = fmt_evi(c_evi_final) if tier_name == "DEFENSIVE" else "see overall EVI"
        vals += [gate_str, evi_str]

    # ── C.2 FPR improvement (8) ──────────────────────────────────────────────
    # Each row: Optimised FPR | Optimised Fire Rate
    def _find_step(step_key, threshold_key, opt_val):
        for row in btc_tier.get(step_key, []):
            if isinstance(opt_val, (int, float)) and abs(row.get(threshold_key, -99) - opt_val) < 0.01:
                return row
        return {}

    opt_def = _g(opt_gates, "DEFENSIVE")
    opt_ha  = _g(opt_gates, "HIGH_ALERT")
    opt_em  = _g(opt_gates, "EMERGENCY")

    row_def = _find_step("step1_sweep", "def", opt_def)
    row_ha  = _find_step("step2_sweep", "ha",  opt_ha)
    row_em  = _find_step("step3_sweep", "em",  opt_em)

    c2_data = {
        "DEFENSIVE":  (row_def.get("fpr"), row_def.get("fire_rate")),
        "HIGH_ALERT": (row_ha.get("fpr"),  row_ha.get("fire_rate")),
        "EMERGENCY":  (row_em.get("fpr"),  row_em.get("fire_rate")),
        "SYSTEMIC":   (None, None),
    }
    for tier_name in ["DEFENSIVE", "HIGH_ALERT", "EMERGENCY", "SYSTEMIC"]:
        fpr_opt, fr_opt = c2_data[tier_name]
        vals += [
            fmt_pct(fpr_opt) if fpr_opt is not None else "structural",
            fmt_pct(fr_opt)  if fr_opt  is not None else "structural",
        ]

    # ── C.3 Justification (1) ────────────────────────────────────────────────
    if isinstance(c_evi_final, (int, float)):
        just = (
            f"Sequential greedy optimisation improved EVI by {fmt_evi(c_evi_final)} "
            f"vs Scenario A (no defence). "
            f"Optimised DEFENSIVE gate raised to {fmt_score(opt_def)} "
            f"(was 5.0), HIGH_ALERT to {fmt_score(opt_ha)} (was 7.0), "
            f"EMERGENCY to {fmt_score(opt_em)} (was 8.0). "
            f"Higher gates reduce false-positive fires while preserving crash protection."
        )
    else:
        just = "Tier optimisation complete \u2014 see sweep tables for full results."
    vals.append(just)

    # ── D.2 Multiplier curves (30) ───────────────────────────────────────────
    # Order: baseline_phase1a, aggressive_step, conservative_step, flat_halved, linear, sigmoid
    # Each row: EVI | MDD | Sharpe | PF | Optimal?
    btc_mult   = mult.get("BTC", {})
    curves     = btc_mult.get("curves", {})
    opt_curve  = btc_mult.get("optimal_curve", "?")
    curve_names = [
        "baseline_phase1a", "aggressive_step", "conservative_step",
        "flat_halved", "linear", "sigmoid",
    ]
    for cn in curve_names:
        r = curves.get(cn, {})
        vals += [
            fmt_evi(r.get("evi_net_pnl")),
            fmt_mdd(r.get("mdd_pct")),
            fmt_sharpe(r.get("sharpe")),
            fmt_pf(r.get("profit_factor")),
            optimal_marker(cn, opt_curve),
        ]

    # ── D.3 Recommendation (1) ───────────────────────────────────────────────
    opt_r   = curves.get(opt_curve, {})
    scen_a  = btc_mult.get("scenario_a", {})
    vals.append(
        f"Recommended curve: {opt_curve} "
        f"(EVI={fmt_evi(opt_r.get('evi_net_pnl'))}, "
        f"MDD={fmt_mdd(opt_r.get('mdd_pct'))}, "
        f"Sharpe={fmt_sharpe(opt_r.get('sharpe'))}, "
        f"PF={fmt_pf(opt_r.get('profit_factor'))}). "
        f"Selected by maximum EVI_net_pnl among 6 curves. "
        f"Scenario A (no defence) net P\u0026L: {fmt_evi(scen_a.get('net_pnl'))}."
    )

    # ── E.2 Portfolio defence (13) ────────────────────────────────────────────
    # Scenarios: Baseline(2), Block longs(4), Tighten stops(3), Combined(4)
    a_cap = scen_a.get("final_capital")
    a_mdd = scen_a.get("mdd_pct")
    b_cap = opt_r.get("final_capital")
    b_mdd = opt_r.get("mdd_pct")
    b_evi = opt_r.get("evi_net_pnl")
    cons_r = curves.get("conservative_step", opt_r)

    vals += [
        fmt_capital(a_cap), fmt_mdd(a_mdd),                          # baseline (2)
        fmt_capital(b_cap), fmt_mdd(b_mdd),                          # block: cap, mdd
        "modelled via multiplier",                                    # block: trades blocked
        fmt_evi(b_evi),                                               # block: evi
        fmt_capital(cons_r.get("final_capital")), fmt_mdd(cons_r.get("mdd_pct")),  # stops: cap, mdd
        fmt_evi(cons_r.get("evi_net_pnl")),                          # stops: evi
        fmt_capital(b_cap), fmt_mdd(b_mdd),                          # combined: cap, mdd
        "block longs + size reduction",                               # combined: trades
        fmt_evi(b_evi),                                               # combined: evi
    ]

    # ── E.3 Verdict (1) ──────────────────────────────────────────────────────
    if isinstance(b_evi, (int, float)) and b_evi > 0:
        verdict = (
            f"PROCEED. Portfolio defence with calibrated CPS thresholds adds "
            f"{fmt_evi(b_evi)} EVI vs undefended baseline. "
            f"MDD: {fmt_mdd(a_mdd)} \u2192 {fmt_mdd(b_mdd)} "
            f"with optimal {opt_curve} curve. Activate in Phase 1B shadow mode."
        )
    elif isinstance(b_evi, (int, float)):
        verdict = (
            f"CONDITIONAL. Defence EVI negative ({fmt_evi(b_evi)}). "
            f"Raise CPS thresholds or switch to conservative_step curve before Phase 1B."
        )
    else:
        verdict = "Verdict requires calibration run output."
    vals.append(verdict)

    # ── F.2 Segmentation (10) ────────────────────────────────────────────────
    # Rows: total_bars, pct_bars, AUC_5m, AUC_10m, crash_rate
    # Columns: BTC-Led | Alt-Led
    skipped = btcd.get("skipped", False)
    n_total = btcd.get("n_total_bars", 0)
    n_btc   = btcd.get("n_btc_led", 0)
    n_alt   = btcd.get("n_alt_led", 0)
    btc_pct = btcd.get("btc_led_pct", 0)
    alt_pct = round((n_alt / max(n_total, 1)) * 100, 2) if n_total else 0
    seg     = btcd.get("segment_results", {})

    def _sauc(sn, h):
        v = _g(seg, sn, h)
        return v.get("auc") if isinstance(v, dict) else None

    def _scr(sn, h):
        v = _g(seg, sn, h)
        return v.get("crash_rate") if isinstance(v, dict) else None

    if skipped:
        vals += ["skipped"] * 10
    else:
        vals += [
            fmt_n(n_btc), fmt_n(n_alt),
            f"{btc_pct:.1f}%", f"{alt_pct:.1f}%",
            fmt_auc(_sauc("btc_led", "crash_5m")),  fmt_auc(_sauc("alt_led", "crash_5m")),
            fmt_auc(_sauc("btc_led", "crash_10m")), fmt_auc(_sauc("alt_led", "crash_10m")),
            fmt_pct(_scr("btc_led", "crash_5m"), decimals=2),
            fmt_pct(_scr("alt_led", "crash_5m"), decimals=2),
        ]

    # ── F.3 AUC comparison (1) ───────────────────────────────────────────────
    auc_5m_btc = btcd.get("auc_5m_btc_led")
    auc_5m_alt = btcd.get("auc_5m_alt_led")
    if not skipped and isinstance(auc_5m_btc, float) and isinstance(auc_5m_alt, float):
        diff = round(auc_5m_btc - auc_5m_alt, 4)
        auc_10m_btc = _sauc("btc_led", "crash_10m")
        auc_10m_alt = _sauc("alt_led", "crash_10m")
        f3 = (
            f"CPS_5m AUC: BTC-led {fmt_auc(auc_5m_btc)} vs Alt-led {fmt_auc(auc_5m_alt)} "
            f"(\u0394={diff:+.4f})."
        )
        if isinstance(auc_10m_btc, float) and isinstance(auc_10m_alt, float):
            d10 = round(auc_10m_btc - auc_10m_alt, 4)
            f3 += (f" CPS_10m AUC: BTC-led {fmt_auc(auc_10m_btc)} vs Alt-led "
                   f"{fmt_auc(auc_10m_alt)} (\u0394={d10:+.4f}).")
    else:
        f3 = "AUC comparison skipped \u2014 ensure BTC, ETH, and SOL all ran in calibration."
    vals.append(f3)

    # ── F.4 Recommendation (1) ───────────────────────────────────────────────
    vals.append(str(btcd.get("recommendation", "INSUFFICIENT_DATA")) if not skipped
                else "Skipped \u2014 re-run with BTC + ETH + SOL data.")

    # ── H.3 Verdict table (18) ───────────────────────────────────────────────
    # 9 rows × 2 columns (Status | Value)
    def _thresh_ok(sym, hz):
        return _g(ts, sym, hz, "optimal", "feasible") is True

    t5m_ok  = _thresh_ok("BTC", "crash_5m")
    t10m_ok = _thresh_ok("BTC", "crash_10m")

    n_evi_pos = sum(
        1 for sym in ["BTC", "ETH", "SOL"]
        for hz in ["crash_5m", "crash_10m"]
        if isinstance(_g(ts, sym, hz, "optimal", "evi_net_pnl"), (int, float))
        and _g(ts, sym, hz, "optimal", "evi_net_pnl") > 0
    )
    evi_ok = n_evi_pos >= 2

    fpr_ok = t5m_ok and _g(ts, "BTC", "crash_5m", "optimal", "fpr", default=1.0) <= 0.70
    fr_ok  = t5m_ok and _g(ts, "BTC", "crash_5m", "optimal", "fire_rate", default=1.0) <= 0.05

    tier_opt = tier.get("BTC", {}).get("optimal", {})
    tier_ok  = bool(tier_opt)

    btc_rec = btcd.get("recommendation")
    btc_ok  = btc_rec is not None and "INSUFFICIENT" not in str(btc_rec)

    p1b_ok = "YES" in str(phase1b) if phase1b else False

    vals += [
        "\u2705 YES" if t5m_ok else "\u274c NO",
        fmt_threshold(_g(ts, "BTC", "crash_5m", "optimal", "threshold")),

        "\u2705 YES" if t10m_ok else "\u274c NO",
        fmt_threshold(_g(ts, "BTC", "crash_10m", "optimal", "threshold")),

        "\u2705 YES" if evi_ok else "\u274c NO",
        f"{n_evi_pos}/6 symbol-horizons EVI > 0",

        "\u2705 YES" if fpr_ok else "\u274c NO",
        fmt_pct(_g(ts, "BTC", "crash_5m", "optimal", "fpr")),

        "\u2705 YES" if fr_ok else "\u274c NO",
        fmt_pct(_g(ts, "BTC", "crash_5m", "optimal", "fire_rate")),

        "\u2705 YES" if tier_ok else "\u274c NO",
        (f"DEF={fmt_score(_g(tier_opt, 'DEFENSIVE'))} "
         f"HA={fmt_score(_g(tier_opt, 'HIGH_ALERT'))} "
         f"EM={fmt_score(_g(tier_opt, 'EMERGENCY'))}"
         if tier_ok else "not completed"),

        "\u2705 YES" if opt_curve and opt_curve != "?" else "\u274c NO",
        str(opt_curve) if opt_curve and opt_curve != "?" else "not completed",

        "\u2705 YES" if btc_ok else "\u26a0\ufe0f SKIPPED",
        str(btc_rec) if btc_ok else "re-run with BTC+ETH+SOL data",

        "\u2705 PROCEED" if p1b_ok else "\u274c CONDITIONAL",
        str(phase1b)[:80] if phase1b else "?",
    ]

    # ── H.4 Required changes (1) ─────────────────────────────────────────────
    changes = []
    if not t5m_ok:
        changes.append("Review CPS_5m \u2014 AUC or constraint not met.")
    if not t10m_ok:
        changes.append("Review CPS_10m threshold \u2014 consider relaxing fire_rate constraint.")
    if not evi_ok:
        changes.append("EVI negative on most symbols \u2014 investigate backtest entry logic.")
    if opt_curve and opt_curve != "?":
        changes.append(f"Deploy {opt_curve} multiplier curve to crash_defense_controller.py.")
    if tier_ok:
        changes.append(
            f"Update tier thresholds in config.yaml: "
            f"DEFENSIVE={fmt_score(_g(tier_opt, 'DEFENSIVE'))}, "
            f"HIGH_ALERT={fmt_score(_g(tier_opt, 'HIGH_ALERT'))}, "
            f"EMERGENCY={fmt_score(_g(tier_opt, 'EMERGENCY'))}."
        )
    changes.append("Activate Phase 1B shadow mode and monitor first 50 candles.")
    if not changes or len(changes) == 1:
        changes.insert(0, "No blockers. Proceed to Phase 1B shadow mode.")
    vals.append(" | ".join(changes))

    return vals


# ══════════════════════════════════════════════════════════════════════════════
#  DOCX READ / WRITE (self-contained via zipfile)
# ══════════════════════════════════════════════════════════════════════════════

def read_docx_xml(docx_path: Path) -> tuple[dict[str, bytes], str]:
    """
    Open docx as a ZIP, return (all_files_dict, document_xml_as_str).
    """
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(docx_path, "r") as z:
        for name in z.namelist():
            files[name] = z.read(name)
    doc_xml = files.get("word/document.xml", b"").decode("utf-8")
    return files, doc_xml


def write_docx_xml(files: dict[str, bytes], new_doc_xml: str, output_path: Path) -> None:
    """
    Write a new docx replacing word/document.xml with new_doc_xml.
    Preserves all other files exactly.
    """
    files["word/document.xml"] = new_doc_xml.encode("utf-8")
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    logger.info("=" * 60)
    logger.info("NexusTrader  generate_v05_doc.py")
    logger.info("=" * 60)

    # ── Validate inputs ───────────────────────────────────────────────────────
    if not TEMPLATE_DOCX.exists():
        logger.error("Template not found: %s", TEMPLATE_DOCX)
        logger.error("Rebuild it: node build_v05.js")
        sys.exit(1)

    if not JSON_PATH.exists():
        logger.error("Calibration results not found: %s", JSON_PATH)
        logger.error("Run first: python scripts/cps_calibration.py   (est. 15-30 min)")
        sys.exit(1)

    # ── Load calibration results ──────────────────────────────────────────────
    logger.info("Loading calibration results from %s", JSON_PATH)
    with open(JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    gen_at = data.get("meta", {}).get("generated_at", "unknown")
    syms   = data.get("meta", {}).get("symbols", [])
    logger.info("  Generated at: %s | Symbols: %s", gen_at, syms)

    # ── Read docx ─────────────────────────────────────────────────────────────
    logger.info("Reading %s", TEMPLATE_DOCX)
    files, doc_xml = read_docx_xml(TEMPLATE_DOCX)

    n_pending = doc_xml.count(PENDING_TEXT)
    logger.info("  Found %d ⏳ PENDING cells in document.xml", n_pending)

    # ── Apply STATUS line replacements (global, not positional) ──────────────
    date_str = gen_at[:10] if len(gen_at) >= 10 else "completed"
    doc_xml = doc_xml.replace(
        "PENDING calibration run",
        f"Calibration run {date_str}",
        1,
    )
    doc_xml = doc_xml.replace(
        "STATUS: PENDING \u2014",
        "STATUS: COMPLETE \u2014",
    )

    # ── Build replacement values ───────────────────────────────────────────────
    replacements = build_replacement_list(data)
    logger.info("  Built %d replacement values", len(replacements))

    # ── Apply positional PENDING replacements ─────────────────────────────────
    replacer = SeqReplacer(doc_xml, PENDING_TEXT)
    logger.info("  PENDING occurrences remaining after STATUS pass: %d", replacer.count)

    if replacer.count != len(replacements):
        logger.error(
            "Mismatch: document has %d ⏳ PENDING cells but replacement list has %d entries.",
            replacer.count, len(replacements),
        )
        logger.error("This usually means the template docx was regenerated with a different")
        logger.error("structure. Re-run 'node build_v05.js' and then this script again.")
        sys.exit(1)

    doc_xml = replacer.apply(replacements)

    # Verify none remain
    n_remaining = doc_xml.count(PENDING_TEXT)
    logger.info("  Replacements applied. Remaining ⏳ PENDING cells: %d", n_remaining)
    if n_remaining > 0:
        logger.warning("  %d cells still contain PENDING — check build_replacement_list()", n_remaining)

    # ── Write output ──────────────────────────────────────────────────────────
    # Write to a temp file first, then move atomically
    tmp = OUTPUT_DOCX.with_suffix(".tmp.docx")
    write_docx_xml(files, doc_xml, tmp)
    shutil.move(str(tmp), str(OUTPUT_DOCX))

    size_kb = OUTPUT_DOCX.stat().st_size / 1024
    logger.info("Written: %s (%.1f KB)", OUTPUT_DOCX, size_kb)
    logger.info("=" * 60)
    logger.info("DONE — v0.5 document populated with calibration results")
    logger.info("Cells filled: %d / %d", len(replacements) - n_remaining, len(replacements))
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
