# ============================================================
# NEXUS TRADER — Intraday Strategy Engine  (Phase 4)
#
# Two-stage signal pipeline:
#   Stage A: Setup Qualification (5m/15m/1h structural conditions)
#   Stage B: Trigger Evaluation (1m/3m precise entry conditions)
#
# Architecture layers consumed: DATA only.
# Architecture layers published: STRATEGY only.
# ZERO PySide6 imports.
# ============================================================
