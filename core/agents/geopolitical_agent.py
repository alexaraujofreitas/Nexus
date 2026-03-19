# ============================================================
# NEXUS TRADER — Geopolitical Risk Agent  (v4 — Full Macro Risk Suite)
#
# Monitors geopolitical, cyber, and public health risk events that
# historically move Bitcoin markets, sourcing data from free public APIs.
#
# ── Architecture ─────────────────────────────────────────────
# The agent uses a multi-layer detection pipeline:
#
#   Layer 1 — Category Keyword Scoring
#     14 keyword categories, each with an independent weight.
#     Partial scores accumulate across all categories and are
#     combined into a composite risk score.
#
#   Layer 2 — Compound Signal Detector
#     Detects co-occurrence of (entity pair + action keyword)
#     in the same article. A compound signal (e.g. Iran +
#     Israel + missile) carries a higher weight than isolated
#     keyword hits, because co-occurrence implies a specific
#     real-world event rather than coincidental mentions.
#
#   Layer 3 — Entity Severity Amplification
#     High-impact geopolitical actors (Iran, North Korea,
#     Taiwan Strait, etc.) multiply the composite score.
#
#   Layer 4 — GDELT Tone Adjustment
#     Global news tone from the GDELT Project acts as a
#     continuous background signal — it confirms or damps
#     the keyword-derived risk score.
#
# ── Signal Output ─────────────────────────────────────────────
#   risk_score      -1.0 … +1.0  (negative = risk-on bearish)
#   signal          same numeric value consumed by orchestrator
#   confidence      0.0 … 1.0
#   risk_level      CRITICAL / HIGH / ELEVATED / MODERATE /
#                   NEUTRAL / CALM
#   signal_direction bearish / neutral / bullish
#   detected_events  list[str] — human-readable event labels
#   category_scores  dict — per-category breakdown
#   compound_signals list[str] — compound events found
#   explanation      str — one-paragraph readable assessment
#
# ── Detection Accuracy Note ───────────────────────────────────
# Current approach: keyword + compound co-occurrence (Layer 1-2)
# Recommended upgrade path → see _DETECTION_NOTES at bottom.
#
# ── Data Sources ─────────────────────────────────────────────
#   GDELT Project       free, no key, global news tone
#   CryptoCompare News  free, no key, crypto + macro headlines
#
# Poll interval: 3600s (1 hour)
# ============================================================
from __future__ import annotations

import logging
import re
import threading
from typing import Any

from core.agents.base_agent import BaseAgent
from core.event_bus import Topics

logger = logging.getLogger(__name__)

_POLL_SECONDS = 3600  # 1 hour


# ─────────────────────────────────────────────────────────────
# CATEGORY 1 — War / Military Escalation
# Weight: 1.00 (highest — direct physical conflict)
# ─────────────────────────────────────────────────────────────
_CAT_MILITARY: dict[str, float] = {
    # Active combat / strikes
    "war":                  0.90,
    "warfare":              0.85,
    "invasion":             0.95,
    "invaded":              0.90,
    "airstrike":            0.90,
    "air strike":           0.90,
    "missile strike":       0.95,
    "missile attack":       0.95,
    "missile":              0.60,
    "bombing":              0.85,
    "bombed":               0.85,
    "shelling":             0.80,
    "ground offensive":     0.90,
    "combat operations":    0.85,
    "military strike":      0.95,
    "naval blockade":       0.85,
    "blockade":             0.70,
    "retaliation":          0.75,
    "attack":               0.30,   # generic — dampened; specific phrases carry full weight
    "troops deployed":      0.80,
    "troop deployment":     0.80,
    # Escalation language
    "escalation":           0.35,   # lowered: generic word, often used in economic/diplomatic contexts
    "military conflict":    0.85,
    "military escalation":  0.90,
    "ceasefire collapse":   0.90,
    "ceasefire collapsed":  0.90,
    "martial law":          0.80,
    "state of emergency":   0.75,
    # Nuclear
    "nuclear":              0.80,
    "nuclear threat":       0.95,
    "nuclear strike":       1.00,
    "tactical nuclear":     0.95,
    "nuclear weapon":       0.90,
    "hypersonic missile":   0.85,
    # Mobilisation
    "nato mobilization":    0.85,
    "nato mobilisation":    0.85,
    "defense alert":        0.75,
    "strategic bombers":    0.80,
    "weapons deployment":   0.80,
}
_CAT_MILITARY_WEIGHT = 1.00

# ─────────────────────────────────────────────────────────────
# CATEGORY 2 — Sanctions / Financial Warfare
# Weight: 0.70
# ─────────────────────────────────────────────────────────────
_CAT_SANCTIONS: dict[str, float] = {
    "sanctions imposed":        0.85,
    "economic sanctions":       0.80,
    "trade sanctions":          0.75,
    "financial sanctions":      0.80,
    "banking sanctions":        0.85,
    "swift ban":                0.90,
    "swift removal":            0.85,
    "asset freeze":             0.80,
    "central bank reserves frozen": 0.90,
    "export ban":               0.70,
    "oil embargo":              0.85,
    "technology embargo":       0.75,
    "secondary sanctions":      0.80,
    "sanctions escalation":     0.85,
    "sanctions package":        0.75,
    "sanctions":                0.55,
    "sanctioned":               0.50,
    "embargo":                  0.65,
}
_CAT_SANCTIONS_WEIGHT = 0.70

# ─────────────────────────────────────────────────────────────
# CATEGORY 3 — Capital Controls / Banking Restrictions
# Weight: 0.65 (direct Bitcoin demand driver — capital flight)
# ─────────────────────────────────────────────────────────────
_CAT_CAPITAL_CONTROLS: dict[str, float] = {
    "capital controls":             0.85,
    "bank withdrawal limits":       0.90,
    "bank run":                     0.85,
    "bank runs":                    0.85,
    "liquidity crisis":             0.80,
    "banking collapse":             0.90,
    "deposit freeze":               0.90,
    "currency controls":            0.80,
    "foreign exchange restrictions":0.80,
    "currency devaluation":         0.75,
    "emergency banking":            0.85,
    "bank holiday":                 0.85,
    "insolvent":                    0.75,
    "insolvency":                   0.75,
    "banking crisis":               0.85,
    "financial contagion":          0.80,
    "contagion":                    0.60,
    "systemic risk":                0.75,
}
_CAT_CAPITAL_CONTROLS_WEIGHT = 0.65

# ─────────────────────────────────────────────────────────────
# CATEGORY 4 — Geopolitical Hotspot Entities
# (Used as per-entity multipliers, not additive scores)
# ─────────────────────────────────────────────────────────────
_HOTSPOT_ENTITIES: dict[str, float] = {
    # Active conflict zones / high-tension actors
    "iran":           1.65,
    "israel":         1.55,
    "ukraine":        1.50,
    "russia":         1.55,
    "china":          1.45,
    "taiwan":         1.60,
    "north korea":    1.65,
    "dprk":           1.65,
    "middle east":    1.50,
    "red sea":        1.45,
    "strait of hormuz": 1.50,
    "taiwan strait":  1.60,
    "south china sea":1.50,
    "gaza":           1.45,
    "hezbollah":      1.50,
    "hamas":          1.45,
    "houthi":         1.40,
    "nato":           1.35,
    # Institutional actors
    "sec":            1.50,
    "cftc":           1.40,
    "fed":            1.30,
    "imf":            1.25,
    "world bank":     1.20,
    "g7":             1.25,
    "g20":            1.20,
    "united nations": 1.20,
    # Crypto systemic
    "binance":        1.50,
    "tether":         1.80,
    "usdt":           1.80,
    "usdc":           1.50,
    # Terror groups
    "isis":           1.55,
    "isil":           1.55,
    "al qaeda":       1.60,
    "al-qaeda":       1.60,
    "taliban":        1.40,
    "hezbollah":      1.50,
    "hamas":          1.45,
    # High-impact geographic disaster zones
    "california":     1.35,   # financial / tech hub + seismic zone
    "tokyo":          1.40,   # major financial center + seismic zone
    "new york":       1.50,   # global financial capital
    "wall street":    1.60,   # direct financial market impact
    "yellowstone":    1.55,   # supervolcano — existential risk
    # Cyber threat actors / targets
    "swift":          1.60,   # SWIFT hack = direct financial system attack
    "power grid":     1.55,   # Grid attack = economic paralysis risk
    "electric grid":  1.55,
    "pipeline":       1.45,   # Colonial Pipeline style events
    "lazarus":        1.60,   # DPRK state hacking group
    "sandworm":       1.55,   # Russian GRU cyber unit
    "fancy bear":     1.50,   # Russian APT28
    "volt typhoon":   1.55,   # Chinese critical infrastructure APT
    # Public health entities
    "who":            1.40,   # WHO emergency = validated outbreak signal
    "world health organization": 1.40,
    "cdc":            1.30,
    "wuhan":          1.55,   # Outbreak origin indicator
    "ebola":          1.50,
    "mpox":           1.35,
    "h5n1":           1.45,   # Highly pathogenic avian influenza
    "disease x":      1.60,   # WHO hypothetical severe pandemic pathogen
}

# ─────────────────────────────────────────────────────────────
# CATEGORY 5 — Energy Supply Disruptions
# Weight: 0.55
# ─────────────────────────────────────────────────────────────
_CAT_ENERGY: dict[str, float] = {
    "oil supply disruption":    0.85,
    "gas pipeline shutdown":    0.80,
    "energy embargo":           0.80,
    "shipping route blockade":  0.80,
    "strait of hormuz":         0.85,
    "red sea shipping":         0.80,
    "tanker attack":            0.80,
    "tanker attacks":           0.80,
    "opec emergency":           0.75,
    "opec cut":                 0.60,
    "oil embargo":              0.80,
    "oil shock":                0.80,
    "energy crisis":            0.75,
    "oil price spike":          0.65,
    "oil surge":                0.55,
}
_CAT_ENERGY_WEIGHT = 0.55

# ─────────────────────────────────────────────────────────────
# CATEGORY 6 — Political Instability / Government Collapse
# Weight: 0.50
# ─────────────────────────────────────────────────────────────
_CAT_POLITICAL: dict[str, float] = {
    "coup":                 0.90,
    "attempted coup":       0.90,
    "government collapse":  0.90,
    "political crisis":     0.70,
    "mass protests":        0.65,
    "civil unrest":         0.65,
    "emergency powers":     0.75,
    "election crisis":      0.70,
    "contested election":   0.65,
    "constitutional crisis":0.80,
    "regime change":        0.85,
    "revolution":           0.85,
    "civil war":            0.95,
    "assassination":        0.80,
}
_CAT_POLITICAL_WEIGHT = 0.50

# ─────────────────────────────────────────────────────────────
# CATEGORY 7 — Global Security Alerts
# Weight: 0.60
# ─────────────────────────────────────────────────────────────
_CAT_SECURITY: dict[str, float] = {
    # Note: terror attack keywords moved to _CAT_TERRORISM (CAT 13)
    # Note: cyber attack keywords moved to _CAT_CYBER (CAT 11)
    # This category now handles residual high-level security events
    # not captured by the dedicated categories above.
    "terror alert":             0.65,
    "homeland security":        0.55,
    "national emergency":       0.75,
    "critical infrastructure":  0.60,
    "grid attack":              0.70,
    "infrastructure attack":    0.70,
    "security threat":          0.50,
    "mass casualty":            0.70,
    "active shooter":           0.60,
    "chemical attack":          0.80,
    "biological attack":        0.85,
    "dirty bomb":               0.90,
    "radiological":             0.85,
}
_CAT_SECURITY_WEIGHT = 0.60

# ─────────────────────────────────────────────────────────────
# CATEGORY 8 — Crypto-Specific Geopolitical Signals
# Weight: 0.65 (direct Bitcoin impact)
# ─────────────────────────────────────────────────────────────
_CAT_CRYPTO_GEO: dict[str, float] = {
    "bitcoin ban":          0.90,
    "crypto ban":           0.90,
    "mining ban":           0.85,
    "crypto regulation emergency":0.85,
    "crypto sanctions":     0.85,
    "cbdc launch":          0.65,
    "cbdc enforcement":     0.75,
    "crypto capital flight":0.70,  # bullish for BTC
    "ban":                  0.45,
    "banned":               0.45,
    "outlawed":             0.75,
    "illegal":              0.55,
    "hack":                 0.70,
    "exploit":              0.70,
    "exchange hack":        0.80,
    "stablecoin depeg":     0.85,
    "depeg":                0.75,
    "exit scam":            0.75,
}
_CAT_CRYPTO_GEO_WEIGHT = 0.65

# ─────────────────────────────────────────────────────────────
# CATEGORY 9 — Global Power Rivalry
# Weight: 0.45 (slower-moving, structural risk)
# ─────────────────────────────────────────────────────────────
_CAT_RIVALRY: dict[str, float] = {
    "trade war":                0.75,
    "tariffs escalation":       0.70,
    "economic retaliation":     0.70,
    "supply chain sanctions":   0.75,
    "semiconductor restrictions":0.70,
    "technology war":           0.65,
    "tech war":                 0.65,
    "decoupling":               0.55,
    "economic coercion":        0.65,
    "tariff":                   0.40,
    "economic war":             0.75,
}
_CAT_RIVALRY_WEIGHT = 0.45

# ─────────────────────────────────────────────────────────────
# CATEGORY 10 — Institutional Triggers
# Weight: 0.35 (confirms / amplifies other signals)
# ─────────────────────────────────────────────────────────────
_CAT_INSTITUTIONAL: dict[str, float] = {
    "un security council":      0.75,
    "security council":         0.60,
    "imf emergency":            0.75,
    "world bank crisis":        0.70,
    "g7 sanctions":             0.80,
    "g20 emergency":            0.75,
    "emergency meeting":        0.60,
    "nato summit":              0.55,
    "nato article 5":           0.90,  # collective defence trigger
    "emergency session":        0.65,
}
_CAT_INSTITUTIONAL_WEIGHT = 0.35

# ─────────────────────────────────────────────────────────────
# CATEGORY 11 — Cyberattack / Cyber Warfare
# Weight: 0.75
#
# Cyber events that matter to Bitcoin:
#   - Financial system / SWIFT attacks  → liquidity shock, BTC safe-haven
#   - Critical infrastructure attacks   → economic disruption, risk-off
#   - Nation-state cyber offensives     → geopolitical escalation proxy
#   - Ransomware on key industries      → supply chain / confidence shock
#
# Sub-categories (all combined into one scoring dict):
#   A. Primary / General Cyber Warfare
#   B. Critical Infrastructure Targets
#   C. Financial System Targets
#   D. Critical Technology Targets
#   E. Malware / Attack Vectors
# ─────────────────────────────────────────────────────────────
_CAT_CYBER: dict[str, float] = {
    # ── A. Primary / General Cyber Warfare ──────────────────
    "cyber attack":                     0.70,
    "cyberattack":                      0.70,
    "cyber warfare":                    0.85,
    "cyber war":                        0.85,
    "cyber sabotage":                   0.80,
    "nation state cyber":               0.90,
    "nation-state cyber":               0.90,
    "state sponsored cyber":            0.90,
    "state-sponsored cyber":            0.90,
    "cyber espionage":                  0.70,
    "cyber offensive":                  0.80,
    "cyber retaliation":                0.80,
    "cyber operation":                  0.70,
    "cyber intrusion":                  0.40,
    "advanced persistent threat":       0.75,
    "apt attack":                       0.75,
    # ── B. Critical Infrastructure Targets ──────────────────
    "power grid attack":                0.95,
    "power grid cyber":                 0.95,
    "electric grid attack":             0.95,
    "electric grid cyber":              0.90,
    "energy infrastructure attack":     0.90,
    "pipeline cyber attack":            0.90,
    "pipeline hack":                    0.85,
    "water system attack":              0.90,
    "water system cyber":               0.90,
    "telecom infrastructure attack":    0.85,
    "satellite system attack":          0.85,
    "transport infrastructure attack":  0.80,
    "hospital cyber attack":            0.75,
    "nuclear plant cyber":              0.95,
    "dam cyber attack":                 0.90,
    # ── C. Financial System Targets ─────────────────────────
    "bank cyber attack":                0.90,
    "banking cyber attack":             0.90,
    "financial network attack":         0.90,
    "stock exchange attack":            0.90,
    "stock exchange cyber":             0.90,
    "payment network attack":           0.85,
    "payment system hack":              0.85,
    "banking system cyber":             0.85,
    "swift cyber attack":               0.95,
    "swift hack":                       0.95,
    "central bank hack":                0.95,
    "atm network attack":               0.80,
    "clearinghouse attack":             0.90,
    # ── D. Critical Technology Targets ──────────────────────
    "cloud provider attack":            0.80,
    "data center attack":               0.80,
    "data center cyber":                0.80,
    "internet backbone attack":         0.90,
    "dns attack":                       0.80,
    "dns infrastructure":               0.75,
    "semiconductor industry hack":      0.75,
    "ai infrastructure attack":         0.75,
    "undersea cable attack":            0.85,
    "communications blackout":          0.80,
    # ── E. Malware / Attack Vectors ─────────────────────────
    "ransomware attack":                0.80,
    "mass ransomware":                  0.90,
    "ransomware":                       0.60,
    "zero day exploit":                 0.80,
    "zero-day exploit":                 0.80,
    "zero day":                         0.65,
    "ddos attack":                      0.70,
    "ddos":                             0.55,
    "distributed denial":               0.70,
    "supply chain attack":              0.85,
    "supply chain hack":                0.85,
    "malware outbreak":                 0.75,
    "wiper malware":                    0.80,
    "destructive malware":              0.80,
    "cyber weapon":                     0.85,
    "exploit deployed":                 0.75,
    "data breach":                      0.30,
    "massive data breach":              0.70,
}
_CAT_CYBER_WEIGHT = 0.75

# ─────────────────────────────────────────────────────────────
# CATEGORY 12 — Pandemic / Public Health Crisis
# Weight: 0.55
#
# How health crises move Bitcoin:
#   - Global lockdowns  → economic collapse fear → risk-off initially,
#     then BTC as inflation hedge (COVID-19 pattern)
#   - Supply chain collapse → commodity shocks → inflation risk
#   - Capital controls during health emergencies → BTC demand
#   - Healthcare system collapse → government spending surge →
#     fiat debasement fear → BTC safe-haven
#
# Sub-categories:
#   A. Outbreak / Pathogen Detection
#   B. WHO / Government Emergency Response
#   C. Economic Shutdown / Lockdown
#   D. Healthcare System Collapse
#   E. Vaccine / Treatment Supply Crisis
# ─────────────────────────────────────────────────────────────
_CAT_PANDEMIC: dict[str, float] = {
    # ── A. Outbreak / Pathogen Detection ────────────────────
    "pandemic":                         0.80,
    "new pandemic":                     0.90,
    "pandemic declared":                0.90,
    "epidemic":                         0.70,
    "global outbreak":                  0.85,
    "disease outbreak":                 0.75,
    "novel virus":                      0.80,
    "new virus":                        0.75,
    "pathogen":                         0.60,
    "airborne disease":                 0.70,
    "highly contagious":                0.70,
    "high mortality":                   0.70,
    "case fatality rate":               0.65,
    "exponential spread":               0.70,
    "uncontrolled spread":              0.75,
    "variant":                          0.50,
    "new variant":                      0.65,
    "lethal variant":                   0.80,
    "bioweapon":                        0.90,
    "biological weapon":                0.90,
    "lab leak":                         0.70,
    # ── B. WHO / Government Emergency Response ──────────────
    "who emergency":                    0.85,
    "public health emergency":          0.85,
    "health emergency":                 0.80,
    "pheic":                            0.90,   # WHO PHEIC declaration
    "international health emergency":   0.90,
    "state of health emergency":        0.85,
    "disease x":                        0.85,
    "pandemic preparedness":            0.55,
    "travel ban":                       0.70,
    "border closure":                   0.75,
    "flight ban":                       0.70,
    "quarantine zone":                  0.75,
    "quarantine":                       0.55,
    "mass quarantine":                  0.80,
    # ── C. Economic Shutdown / Lockdown ─────────────────────
    "lockdown":                         0.75,
    "national lockdown":                0.85,
    "global lockdown":                  0.90,
    "economic shutdown":                0.85,
    "factory shutdown":                 0.70,
    "supply chain collapse":            0.85,
    "supply chain disruption":          0.65,
    "port closure":                     0.70,
    "economic paralysis":               0.80,
    "recession fears":                  0.60,
    "demand shock":                     0.65,
    "consumption collapse":             0.70,
    # ── D. Healthcare System Collapse ───────────────────────
    "hospital overwhelmed":             0.80,
    "healthcare collapse":              0.85,
    "icu capacity":                     0.65,
    "health system collapse":           0.85,
    "mass casualties":                  0.80,
    "mass death":                       0.85,
    "excess mortality":                 0.70,
    "morgue overflow":                  0.80,
    # ── E. Government Fiscal Response ───────────────────────
    "emergency stimulus":               0.65,
    "helicopter money":                 0.70,
    "money printing":                   0.65,
    "bailout package":                  0.60,
    "debt monetization":                0.70,
    "hyperinflation":                   0.80,
    "inflation surge":                  0.65,
}
_CAT_PANDEMIC_WEIGHT = 0.55

# ─────────────────────────────────────────────────────────────
# CATEGORY 13 — Terrorism Events
# Weight: 0.70
#
# Terrorism moves markets through:
#   - Immediate risk-off shock (flight to safety)
#   - Targeting financial centers → direct liquidity disruption
#   - Coordinated multi-site attacks → systemic fear premium
#   - Infrastructure terrorism → physical economic damage
#
# Sub-categories:
#   A. Primary Terrorism Events
#   B. Infrastructure Terrorism
#   C. Organised Terror Groups
#   D. High-Impact Scenarios
# ─────────────────────────────────────────────────────────────
_CAT_TERRORISM: dict[str, float] = {
    # ── A. Primary Terrorism Events ─────────────────────────
    # Weights are intentionally moderate — compound signals provide
    # the additional score that elevates major attacks to HIGH/CRITICAL.
    # This prevents single isolated incidents from over-triggering.
    "terror attack":                0.55,
    "terrorist attack":             0.55,
    "terror bombing":               0.60,
    "suicide bombing":              0.60,
    "suicide bomber":               0.55,
    "mass shooting":                0.50,
    "vehicle ramming":              0.45,
    "car bombing":                  0.55,
    "truck attack":                 0.45,
    "airport attack":               0.65,
    "train station attack":         0.60,
    "stadium attack":               0.60,
    "concert attack":               0.55,
    "hotel attack":                 0.55,
    "shopping mall attack":         0.50,
    "school attack":                0.45,
    "hostage crisis":               0.65,
    "hostage situation":            0.55,
    "mass hostage":                 0.70,
    "extremist attack":             0.55,
    "militant attack":              0.55,
    "jihad":                        0.45,
    "jihadist":                     0.45,
    # ── B. Infrastructure Terrorism ──────────────────────────
    # Infrastructure attacks carry higher base weight as they
    # directly impact markets regardless of compound detection.
    "pipeline attack":              0.80,
    "power grid terrorism":         0.85,
    "port attack":                  0.75,
    "airport shutdown":             0.65,
    "financial district attack":    0.90,
    "stock exchange attack":        0.90,
    "bank attack":                  0.75,
    "transport attack":             0.60,
    "bridge attack":                0.60,
    "dam attack":                   0.75,
    # ── C. Organised Terror Groups ───────────────────────────
    "isis":                         0.60,
    "isil":                         0.60,
    "islamic state":                0.60,
    "al qaeda":                     0.65,
    "al-qaeda":                     0.65,
    "terror network":               0.60,
    "extremist cell":               0.55,
    "terror cell":                  0.55,
    "boko haram":                   0.50,
    "taliban":                      0.50,
    "hezbollah attack":             0.70,
    "hamas attack":                 0.70,
    # ── D. High-Impact Scenarios ─────────────────────────────
    "coordinated terror":           0.85,
    "coordinated attacks":          0.80,
    "multiple terror attacks":      0.85,
    "simultaneous attacks":         0.80,
    "large scale terror":           0.80,
    "mass casualty event":          0.70,
    "terrorism":                    0.35,
    "terrorist":                    0.30,
}
_CAT_TERRORISM_WEIGHT = 0.70

# ─────────────────────────────────────────────────────────────
# CATEGORY 14 — Assassination Attempts on Major Leaders
# Weight: 0.80
#
# Assassinations / attempts create political vacuum, leadership
# uncertainty, and market shock — especially for:
#   - Leaders of nuclear powers        → max severity
#   - Leaders of major economies (G7)  → high severity
#   - Central bank governors           → direct financial impact
#   - Crypto-friendly / hostile heads  → direct BTC impact
# ─────────────────────────────────────────────────────────────
_CAT_ASSASSINATION: dict[str, float] = {
    # Assassination outcomes
    "assassination":                    0.90,
    "assassinated":                     0.95,
    "assassin":                         0.80,
    "assassination attempt":            0.95,
    "attempted assassination":          0.95,
    "assassination plot":               0.85,
    "leader killed":                    0.95,
    "president killed":                 0.95,
    "prime minister killed":            0.95,
    "head of state killed":             0.95,
    # Attack language
    "leader shot":                      0.90,
    "president shot":                   0.95,
    "prime minister shot":              0.95,
    "leader attacked":                  0.85,
    "president attacked":               0.90,
    "political assassination":          0.90,
    "political leader killed":          0.95,
    "coup assassination":               0.95,
    # High-impact leader-specific
    "us president assassination":       1.00,
    "chinese president assassination":  1.00,
    "russian president assassination":  1.00,
    "nato leader assassination":        0.95,
    "eu leader assassination":          0.90,
    "central bank governor":            0.80,   # "...attacked/killed" in compound
    # Near-miss / threat
    "leader evacuation":                0.70,
    "president security threat":        0.70,
    "credible assassination threat":    0.80,
    "bomb near leader":                 0.85,
    "drone attack leader":              0.85,
}
_CAT_ASSASSINATION_WEIGHT = 0.80

# ─────────────────────────────────────────────────────────────
# CATEGORY 15 — Large-Scale Natural Disasters
# Weight: 0.45
#
# Natural disasters move Bitcoin through:
#   - Supply chain disruption (factories, ports, mining)
#   - Insurance / reinsurance liquidity stress
#   - Government emergency spending → inflation risk
#   - Power grid / infrastructure collapse → mining impact
#   - US events disproportionately impact global markets
#
# Sub-categories:
#   A. Earthquakes
#   B. Tsunamis
#   C. Hurricanes / Typhoons / Cyclones
#   D. Volcanic Events
#   E. Critical US / G7 Financial Center Events
# ─────────────────────────────────────────────────────────────
_CAT_NATURAL_DISASTER: dict[str, float] = {
    # ── A. Earthquakes ───────────────────────────────────────
    "major earthquake":             0.80,
    "massive earthquake":           0.85,
    "earthquake disaster":          0.85,
    "earthquake damage":            0.70,
    "high magnitude earthquake":    0.85,
    "magnitude 7":                  0.75,
    "magnitude 8":                  0.85,
    "magnitude 9":                  0.95,
    "city level earthquake":        0.90,
    "infrastructure collapse earthquake": 0.90,
    "building collapse earthquake": 0.80,
    "earthquake emergency":         0.80,
    "seismic disaster":             0.80,
    # ── B. Tsunamis ──────────────────────────────────────────
    "tsunami warning":              0.85,
    "major tsunami":                0.90,
    "coastal tsunami":              0.85,
    "tsunami damage":               0.90,
    "tsunami evacuation":           0.80,
    "tsunami alert":                0.80,
    "tsunami":                      0.65,
    "tidal wave":                   0.75,
    # ── C. Hurricanes / Typhoons / Cyclones ──────────────────
    "category 5 hurricane":         0.90,
    "category 4 hurricane":         0.80,
    "super typhoon":                0.90,
    "extreme cyclone":              0.85,
    "devastating storm":            0.75,
    "storm catastrophe":            0.80,
    "hurricane landfall":           0.75,
    "flood catastrophe":            0.80,
    "severe flooding":              0.65,
    "storm surge":                  0.65,
    "extreme weather disaster":     0.70,
    # ── D. Volcanic Events ───────────────────────────────────
    "volcanic eruption":            0.80,
    "major volcanic eruption":      0.90,
    "volcano disaster":             0.85,
    "ash cloud":                    0.70,
    "aviation shutdown":            0.75,
    "lava flow damage":             0.70,
    "supervolcano":                 0.95,
    # ── E. US / G7 Financial Center Events ───────────────────
    "california earthquake":        0.90,
    "san francisco earthquake":     0.95,
    "los angeles earthquake":       0.95,
    "new york disaster":            0.95,
    "us west coast tsunami":        0.95,
    "new madrid fault":             0.90,
    "yellowstone":                  0.90,
    "hurricane new york":           0.90,
    "hurricane miami":              0.85,
    "hurricane houston":            0.85,
    "tokyo earthquake":             0.90,
    "london disaster":              0.85,
}
_CAT_NATURAL_DISASTER_WEIGHT = 0.45

# ─────────────────────────────────────────────────────────────
# CALM / BULLISH SIGNALS (reduce risk score)
# ─────────────────────────────────────────────────────────────
_CAT_CALM: dict[str, float] = {
    "etf approved":             -0.50,
    "spot etf":                 -0.45,
    "institutional adoption":   -0.35,
    "legal clarity":            -0.40,
    "peace deal":               -0.55,
    "ceasefire agreement":      -0.55,
    "de-escalation":            -0.50,
    "diplomatic resolution":    -0.50,
    "sanctions lifted":         -0.50,
    "trade deal":               -0.35,
    "protocol upgrade":         -0.20,
    "mainnet":                  -0.15,
    "approved":                 -0.15,
    "licensed":                 -0.20,
}

# All scoring categories: (keyword_dict, category_weight, label)
_CATEGORIES: list[tuple[dict[str, float], float, str]] = [
    (_CAT_MILITARY,          _CAT_MILITARY_WEIGHT,          "military_escalation"),
    (_CAT_SANCTIONS,         _CAT_SANCTIONS_WEIGHT,         "sanctions"),
    (_CAT_CAPITAL_CONTROLS,  _CAT_CAPITAL_CONTROLS_WEIGHT,  "capital_controls"),
    (_CAT_ENERGY,            _CAT_ENERGY_WEIGHT,            "energy_disruption"),
    (_CAT_POLITICAL,         _CAT_POLITICAL_WEIGHT,         "political_instability"),
    (_CAT_SECURITY,          _CAT_SECURITY_WEIGHT,          "security_alerts"),
    (_CAT_CRYPTO_GEO,        _CAT_CRYPTO_GEO_WEIGHT,        "crypto_geopolitical"),
    (_CAT_RIVALRY,           _CAT_RIVALRY_WEIGHT,           "power_rivalry"),
    (_CAT_INSTITUTIONAL,     _CAT_INSTITUTIONAL_WEIGHT,     "institutional"),
    (_CAT_CYBER,             _CAT_CYBER_WEIGHT,             "cyberattack"),
    (_CAT_PANDEMIC,          _CAT_PANDEMIC_WEIGHT,          "pandemic_health"),
    (_CAT_TERRORISM,         _CAT_TERRORISM_WEIGHT,         "terrorism"),
    (_CAT_ASSASSINATION,     _CAT_ASSASSINATION_WEIGHT,     "assassination"),
    (_CAT_NATURAL_DISASTER,  _CAT_NATURAL_DISASTER_WEIGHT,  "natural_disaster"),
]


# ─────────────────────────────────────────────────────────────
# COMPOUND SIGNAL DEFINITIONS
#
# A compound signal fires when ALL of the following are found
# in the combined text:
#   - at least one entity from `entities`
#   - at least one action from `actions`
#
# Compound signals carry extra_weight added directly to the
# raw risk score, bypassing category weighting.  They represent
# specific real-world events, not coincidental keyword presence.
# ─────────────────────────────────────────────────────────────
_COMPOUND_SIGNALS: list[dict] = [
    {
        "label":        "Iran–Israel military exchange",
        "entities":     {"iran", "israel"},
        "actions":      {"strike", "airstrike", "missile", "attack", "retaliation",
                         "bombing", "war", "escalation"},
        "extra_weight": 0.55,
        "min_entities": 2,   # both Iran AND Israel must be mentioned
        "signal_note":  "Active Iran-Israel military exchange — extreme risk-off event for crypto",
    },
    {
        "label":        "China–Taiwan military escalation",
        "entities":     {"china", "taiwan"},
        "actions":      {"military", "drills", "blockade", "invasion", "exercises",
                         "encirclement", "forces", "warships", "jets"},
        "extra_weight": 0.60,
        "min_entities": 2,   # both China AND Taiwan must be mentioned
        "signal_note":  "China-Taiwan military escalation — highest geopolitical risk for markets",
    },
    {
        "label":        "Russia–NATO confrontation",
        "entities":     {"russia", "nato"},
        "actions":      {"troops", "nuclear", "conflict", "border", "confrontation",
                         "mobilization", "mobilisation", "invasion", "escalation"},
        "extra_weight": 0.50,
        "min_entities": 2,   # both Russia AND NATO must be mentioned
        "signal_note":  "Russia-NATO confrontation — systemic European security risk",
    },
    {
        "label":        "Russia–Ukraine escalation",
        "entities":     {"russia", "ukraine"},
        "actions":      {"airstrike", "offensive", "invasion", "attack", "bombing",
                         "shelling", "advance", "occupation", "strike"},
        "extra_weight": 0.40,
        "min_entities": 2,   # both Russia AND Ukraine must be mentioned
        "signal_note":  "Russia-Ukraine active escalation — risk-off pressure on crypto",
    },
    {
        "label":        "DPRK nuclear/missile threat",
        "entities":     {"north korea", "dprk"},
        "actions":      {"nuclear", "missile", "test", "launch", "icbm", "weapon",
                         "warhead", "threat"},
        "extra_weight": 0.50,
        "min_entities": 1,   # either name alone is sufficient (rare, high-specificity entity)
        "signal_note":  "North Korea nuclear or missile provocation — regional destabilisation",
    },
    {
        "label":        "Red Sea / Hormuz shipping crisis",
        "entities":     {"red sea", "strait of hormuz", "houthi"},
        "actions":      {"attack", "blockade", "tanker", "shipping", "disruption",
                         "seizure", "drone", "missile"},
        "extra_weight": 0.35,
        "min_entities": 1,
        "signal_note":  "Critical shipping lane disruption — energy price shock risk",
    },
    {
        "label":        "US–China tech/trade war",
        "entities":     {"china", "us"},
        "actions":      {"tariff", "semiconductor", "ban", "restriction",
                         "trade war", "retaliation", "decoupling"},
        "extra_weight": 0.35,
        "min_entities": 2,   # both US AND China must be mentioned
        "signal_note":  "US-China trade/tech confrontation — global growth risk",
    },
    {
        "label":        "Crypto capital controls / ban",
        "entities":     {"bitcoin", "crypto", "cryptocurrency"},
        "actions":      {"ban", "banned", "outlawed", "seizure", "capital controls",
                         "sanctions", "illegal", "prohibited"},
        "extra_weight": 0.45,
        "signal_note":  "Direct crypto ban or capital controls — immediate sell pressure",
    },
    {
        "label":        "SWIFT / financial system exclusion",
        "entities":     {"swift", "banking", "central bank"},
        "actions":      {"ban", "exclusion", "frozen", "sanctions", "blocked",
                         "disconnected", "removed"},
        "extra_weight": 0.40,
        "signal_note":  "SWIFT or banking system exclusion — capital flight to BTC likely",
    },
    {
        "label":        "Stablecoin systemic crisis",
        "entities":     {"tether", "usdt", "usdc", "stablecoin"},
        "actions":      {"depeg", "depegged", "collapse", "freeze", "seized",
                         "insolvent", "bankrupt", "ban"},
        "extra_weight": 0.50,
        "signal_note":  "Stablecoin depeg or freeze — systemic crypto market risk",
    },

    # ── Cyberattack Compound Signals ──────────────────────────
    {
        "label":        "Nation-State Financial System Cyberattack",
        "entities":     {"swift", "bank", "banking", "central bank",
                         "stock exchange", "payment network", "clearinghouse"},
        "actions":      {"cyber attack", "cyberattack", "hack", "attack",
                         "cyber warfare", "ransomware", "zero day", "intrusion"},
        "extra_weight": 0.60,
        "signal_note":  "Financial system cyberattack detected — liquidity shock risk, potential BTC safe-haven surge",
    },
    {
        "label":        "Critical Infrastructure Cyberattack",
        "entities":     {"power grid", "electric grid", "pipeline", "water system",
                         "energy infrastructure", "nuclear", "dam", "hospital"},
        "actions":      {"cyber attack", "cyberattack", "attack", "hack",
                         "ransomware", "sabotage", "intrusion", "zero day"},
        "extra_weight": 0.55,
        "signal_note":  "Critical infrastructure under cyberattack — economic disruption, risk-off macro environment",
    },
    {
        "label":        "Nation-State Cyber Warfare Escalation",
        "entities":     {"russia", "china", "iran", "north korea", "dprk",
                         "nato", "us", "ukraine", "israel"},
        "actions":      {"cyber warfare", "cyber war", "cyber attack", "cyber offensive",
                         "cyber retaliation", "state sponsored", "nation state",
                         "apt attack", "cyber weapon"},
        "extra_weight": 0.50,
        "signal_note":  "Nation-state cyber warfare escalation — geopolitical proxy conflict, macro risk event",
    },
    {
        "label":        "Mass Ransomware / Global Cyber Incident",
        "entities":     {"ransomware", "malware", "cyber attack", "cyberattack"},
        "actions":      {"mass", "global", "widespread", "critical", "major",
                         "multiple countries", "systemic", "coordinated"},
        "extra_weight": 0.40,
        "signal_note":  "Mass coordinated cyberattack event — potential economic disruption across multiple sectors",
    },

    # ── Pandemic Compound Signals ─────────────────────────────
    {
        "label":        "WHO Global Pandemic Emergency",
        "entities":     {"who", "world health organization", "cdc", "health authority"},
        "actions":      {"pandemic", "pheic", "emergency", "public health emergency",
                         "global outbreak", "pandemic declared", "disease x"},
        "extra_weight": 0.55,
        "signal_note":  "WHO pandemic emergency declaration — severe economic disruption risk, initial risk-off then BTC inflation hedge",
    },
    {
        "label":        "Global Economic Lockdown",
        "entities":     {"lockdown", "shutdown", "quarantine"},
        "actions":      {"national", "global", "worldwide", "economy", "economic",
                         "factories", "supply chain", "ports", "borders"},
        "extra_weight": 0.50,
        "signal_note":  "Economic lockdown detected — supply chain collapse risk, massive fiscal stimulus likely (BTC inflation hedge)",
    },
    {
        "label":        "Novel Pathogen Outbreak",
        "entities":     {"virus", "pathogen", "disease", "outbreak"},
        "actions":      {"novel", "new", "unknown", "highly contagious", "airborne",
                         "exponential", "uncontrolled", "rapid spread", "lethal"},
        "extra_weight": 0.45,
        "signal_note":  "Novel pathogen outbreak with rapid spread — early-stage pandemic risk, monitor for escalation",
    },
    {
        "label":        "Bioweapon / Engineered Pathogen Event",
        "entities":     {"bioweapon", "biological weapon", "lab leak", "engineered virus",
                         "gain of function"},
        "actions":      {"attack", "release", "leaked", "escaped", "deployed",
                         "confirmed", "suspected", "investigated"},
        "extra_weight": 0.65,
        "signal_note":  "Bioweapon or engineered pathogen event — extreme macro risk, safe-haven demand surge expected",
    },
    {
        "label":        "Pandemic Fiscal Stimulus Surge",
        "entities":     {"stimulus", "money printing", "helicopter money",
                         "quantitative easing", "debt monetization"},
        "actions":      {"pandemic", "health crisis", "lockdown", "emergency",
                         "unprecedented", "massive", "trillion"},
        "extra_weight": 0.35,
        "signal_note":  "Pandemic-driven fiscal stimulus — fiat debasement risk, historically bullish for Bitcoin",
    },

    # ── Terrorism Compound Signals ────────────────────────────
    {
        "label":        "Financial District Terror Attack",
        "entities":     {"financial district", "stock exchange", "wall street",
                         "bank", "trading floor", "clearinghouse", "federal reserve"},
        "actions":      {"terror attack", "terrorist attack", "bombing",
                         "explosion", "shooting", "hostage"},   # "attack" removed — too generic (matches cyber)
        "extra_weight": 0.65,
        "signal_note":  "Terror attack on financial infrastructure — severe liquidity shock, extreme risk-off event",
    },
    {
        "label":        "Major Airport / Transport Terror Attack",
        "entities":     {"airport", "train station", "subway", "metro",
                         "port", "harbour", "harbor"},
        "actions":      {"terror attack", "terrorist attack", "bombing",
                         "explosion", "shooting", "shutdown"},   # "attack" removed — too generic
        "extra_weight": 0.50,
        "signal_note":  "Major transport hub terror attack — global logistics disruption, short-term risk-off shock",
    },
    {
        "label":        "Coordinated Multi-Site Terror Event",
        "entities":     {"coordinated terror", "coordinated attacks", "multiple terror",
                         "simultaneous attacks", "large scale terror"},
        "actions":      {"cities", "countries", "financial", "airports",
                         "governments", "infrastructure"},
        "extra_weight": 0.60,
        "signal_note":  "Coordinated multi-site terror event — systemic security shock, elevated safe-haven demand",
    },
    {
        "label":        "Critical Infrastructure Terror Attack",
        "entities":     {"power grid", "pipeline", "water system", "dam",
                         "nuclear plant", "telecom"},
        "actions":      {"terror attack", "terrorist", "bombing", "destroyed",
                         "sabotage", "explosion", "attack"},
        "extra_weight": 0.55,
        "signal_note":  "Critical infrastructure terror attack — physical economic damage, risk-off pressure",
    },

    # ── Assassination Compound Signals ────────────────────────
    {
        "label":        "Major World Leader Assassinated / Attacked",
        "entities":     {"president", "prime minister", "chancellor", "head of state",
                         "leader", "secretary general"},
        "actions":      {"assassinated", "assassination", "killed", "shot",
                         "attacked", "assassination attempt", "attempted assassination"},
        "extra_weight": 0.75,
        "signal_note":  "World leader assassination or attempt — extreme political uncertainty, maximum risk-off shock",
    },
    {
        "label":        "Nuclear Power Leader Assassination",
        "entities":     {"us president", "russian president", "chinese president",
                         "putin", "xi jinping", "biden", "trump"},
        "actions":      {"assassinated", "assassination", "killed", "shot",
                         "attacked", "assassination attempt", "dead"},
        "extra_weight": 0.90,
        "signal_note":  "CRITICAL: Nuclear power leader assassinated — systemic global instability, immediate safe-haven surge expected",
    },
    {
        "label":        "Central Bank Governor Targeted",
        "entities":     {"federal reserve", "fed chair", "ecb president",
                         "central bank governor", "bank of england"},
        "actions":      {"assassination", "attacked", "killed", "shot",
                         "targeted", "assassination attempt"},
        "extra_weight": 0.60,
        "signal_note":  "Central bank leadership targeted — monetary policy uncertainty, financial market shock",
    },

    # ── Natural Disaster Compound Signals ─────────────────────
    {
        "label":        "Major Earthquake in Financial Center",
        "entities":     {"california", "san francisco", "los angeles", "seattle",
                         "tokyo", "new york", "london", "singapore", "hong kong"},
        "actions":      {"earthquake", "major earthquake", "magnitude", "seismic",
                         "infrastructure collapse", "building collapse"},
        "extra_weight": 0.50,
        "signal_note":  "Major earthquake in global financial center — infrastructure damage, supply chain disruption risk",
    },
    {
        "label":        "US Coastal Tsunami Event",
        "entities":     {"tsunami", "tidal wave"},
        "actions":      {"california", "oregon", "washington", "hawaii", "alaska",
                         "us west coast", "pacific coast", "east coast", "new york"},
        "extra_weight": 0.55,
        "signal_note":  "Tsunami threatening US coastal financial centers — extreme infrastructure and economic risk",
    },
    {
        "label":        "Hurricane Hitting Major Economic Hub",
        "entities":     {"hurricane", "typhoon", "cyclone", "super typhoon"},
        "actions":      {"new york", "houston", "miami", "new orleans", "tokyo",
                         "shanghai", "port", "financial", "oil refinery"},
        "extra_weight": 0.40,
        "signal_note":  "Major storm hitting economic hub — insurance stress, supply chain disruption, energy price risk",
    },
    {
        "label":        "Disaster + Infrastructure Collapse",
        "entities":     {"earthquake", "tsunami", "hurricane", "volcanic eruption",
                         "flood", "disaster"},
        "actions":      {"infrastructure collapse", "power outage", "grid failure",
                         "port closure", "airport closed", "supply chain",
                         "communication blackout", "financial system"},
        "extra_weight": 0.45,
        "signal_note":  "Natural disaster causing infrastructure collapse — economic paralysis risk, safe-haven demand likely",
    },
]

# ─────────────────────────────────────────────────────────────
# Risk level thresholds (composite risk score → label)
# ─────────────────────────────────────────────────────────────
_RISK_THRESHOLDS = [
    # CRITICAL: reserved for compound-confirmed or multi-source extreme events
    # (requires score ≥ 0.90, typically needs compound signals to reach this level)
    (0.90,  "CRITICAL",  -0.90),
    # HIGH: serious single-category events or moderate compound events
    (0.65,  "HIGH",      -0.70),
    # ELEVATED: notable risk — multiple keyword hits or moderate events
    (0.40,  "ELEVATED",  -0.45),
    # MODERATE: early-warning — some risk keywords present
    (0.20,  "MODERATE",  -0.22),
    # NEUTRAL: background noise only
    (-0.05, "NEUTRAL",    0.00),
    # CALM: positive environment — mild bullish tailwind
    (-9.99, "CALM",      +0.20),
]


# ─────────────────────────────────────────────────────────────
# DETECTION NOTES (for future upgrade path)
# ─────────────────────────────────────────────────────────────
_DETECTION_NOTES = """
APPROACH EVALUATION — Keyword vs NLP vs Hybrid
================================================

Current approach: keyword scoring + compound co-occurrence.

Strengths:
  - Zero latency, no model overhead, deterministic.
  - Compound detection catches specific high-impact events.
  - Easy to audit, explain, and tune.

Weaknesses:
  - Cannot understand context: "peace talks ended the war" vs
    "war declared" both trigger "war".
  - Single-article co-occurrence is noisy. "China said Taiwan
    needs to avoid military drills" may fire the compound signal.
  - Misses paraphrase: "launched a barrage of rockets" won't match
    "missile strike" unless "rockets" is in the keyword list.

RECOMMENDED UPGRADE: Hybrid approach
  1. Keep keyword/compound as the fast first-pass filter.
  2. For any article scoring above a threshold, pass the full
     text through a sentence-level classifier:
       - Fine-tuned BERT/DistilBERT on conflict news corpus
       - OR few-shot prompt to local Ollama LLM (already present
         in NexusTrader) asking: "Does this headline indicate a
         new or escalating geopolitical risk? Score 0-10."
  3. Use the LLM/model output to confirm or veto the keyword signal.

ADDITIONAL GEOPOLITICAL INDICATORS TO CONSIDER:
  - VIX spike > 30 (correlated with safe-haven demand for BTC)
  - Gold price surge > 2% in 24h (parallel safe-haven indicator)
  - DXY spike > 1% (risk-off dollar demand)
  - Oil price spike > 5% in 24h (energy shock proxy)
  - Sovereign CDS spread widening (country default fear)
  - Twitter/X trending topics via X API (fastest signal source)
  - Telegram channel monitoring (faster than CryptoCompare)
  - Al Jazeera / Reuters RSS feeds (broader geopolitical coverage)
  These could all be added as additional fetch() sources and folded
  into the composite score with appropriate weights.
"""


class GeopoliticalAgent(BaseAgent):
    """
    Monitors geopolitical, cyber warfare, and public health risk from
    global news signals.

    Uses a 14-category keyword framework with compound signal detection
    to produce a directional risk signal for the orchestrator:

      negative signal → elevated geopolitical risk → risk-off → avoid longs
      positive signal → calm / positive regulatory → mild bullish tailwind

    Output keys consumed by the orchestrator:
      signal, confidence, risk_score, risk_level, signal_direction,
      detected_events, compound_signals, category_scores, explanation
    """

    def __init__(self, parent=None):
        super().__init__("geopolitical", parent)
        self._lock  = threading.RLock()
        self._cache: dict = {}

    # ── BaseAgent interface ────────────────────────────────────

    @property
    def event_topic(self) -> str:
        return Topics.SOCIAL_SIGNAL   # differentiated by source="geopolitical"

    @property
    def poll_interval_seconds(self) -> int:
        return _POLL_SECONDS

    def fetch(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}

        try:
            raw["gdelt"] = self._fetch_gdelt()
        except Exception as exc:
            logger.debug("GeopoliticalAgent: GDELT skipped — %s", exc)

        try:
            raw["cc_headlines"] = self._fetch_cc_headlines()
        except Exception as exc:
            logger.debug("GeopoliticalAgent: CC headlines skipped — %s", exc)

        return raw

    # ── Core processing ───────────────────────────────────────

    def process(self, raw: dict) -> dict:
        headlines:  list[str] = raw.get("cc_headlines", [])
        gdelt:      dict      = raw.get("gdelt", {})
        gdelt_tone: float     = gdelt.get("avg_tone", 0.0) if gdelt else 0.0

        # Build combined text (lower-cased) for scanning
        combined = " ".join(h.lower() for h in headlines)

        # ── Layer 1: Category scoring ─────────────────────────
        category_scores: dict[str, float] = {}
        risk_score = 0.0
        detected_events: list[str] = []

        for kw_dict, cat_weight, cat_label in _CATEGORIES:
            cat_raw, cat_events = self._score_category(combined, kw_dict, cat_label)
            weighted = cat_raw * cat_weight
            category_scores[cat_label] = round(weighted, 4)
            risk_score += weighted
            detected_events.extend(cat_events)

        # Calm signals subtract from risk score
        calm_raw, calm_events = self._score_category(combined, _CAT_CALM, "calm")
        risk_score += calm_raw   # calm values are already negative
        category_scores["calm_positive"] = round(calm_raw, 4)

        # ── Layer 2: Compound signal detection ────────────────
        compound_signals: list[str] = []
        compound_notes:   list[str] = []
        for cs in _COMPOUND_SIGNALS:
            fired, note = self._check_compound(combined, cs)
            if fired:
                risk_score  += cs["extra_weight"]
                compound_signals.append(cs["label"])
                compound_notes.append(note)
                detected_events.append(f"⚠ Compound: {cs['label']}")

        # ── Layer 3: Entity severity amplification ────────────
        # Only apply when there is meaningful military/security scoring (>= 0.50).
        # This prevents purely economic events (sanctions, trade disputes) from
        # being inflated to CRITICAL just because a hotspot entity is mentioned.
        entity_multiplier, active_entities = self._entity_multiplier(combined)
        mil_score = category_scores.get("military_escalation", 0.0)
        sec_score = category_scores.get("security_alerts", 0.0)
        if entity_multiplier > 1.0 and risk_score > 0 and (mil_score >= 0.50 or sec_score >= 0.40):
            risk_score *= entity_multiplier

        # ── Layer 4: GDELT tone adjustment ────────────────────
        gdelt_adjustment = 0.0
        if gdelt:
            if gdelt_tone < -5.0:
                gdelt_adjustment = +0.10   # very negative global tone → more risk
                detected_events.insert(0, f"GDELT: Very negative global tone ({gdelt_tone:.1f})")
            elif gdelt_tone < -2.0:
                gdelt_adjustment = +0.05
                detected_events.insert(0, f"GDELT: Negative global tone ({gdelt_tone:.1f})")
            elif gdelt_tone > 5.0:
                gdelt_adjustment = -0.08   # positive global tone → damp risk
                detected_events.insert(0, f"GDELT: Very positive global tone ({gdelt_tone:.1f})")
        risk_score += gdelt_adjustment

        # Clamp risk score to [-1, +1]
        risk_score = max(-1.0, min(1.0, risk_score))

        # ── Map risk score → level + signal + direction ───────
        risk_level, signal = self._map_risk_level(risk_score)
        signal_direction = (
            "bearish"  if signal < -0.05 else
            "bullish"  if signal >  0.05 else
            "neutral"
        )

        # ── Confidence model ──────────────────────────────────
        confidence = self._compute_confidence(
            has_gdelt       = bool(gdelt),
            n_events        = len([e for e in detected_events if not e.startswith("GDELT")]),
            n_compounds     = len(compound_signals),
            entity_mult     = entity_multiplier,
            has_headlines   = bool(headlines),
        )

        # ── Build explanation ─────────────────────────────────
        explanation = self._build_explanation(
            risk_level, signal, risk_score, confidence,
            detected_events, compound_signals, compound_notes,
            active_entities, gdelt_tone, gdelt,
        )

        result = {
            # Orchestrator-consumed fields
            "signal":           round(signal,     4),
            "confidence":       round(confidence, 4),
            # Extended geopolitical fields
            "risk_score":       round(risk_score,   4),
            "risk_level":       risk_level,
            "signal_direction": signal_direction,
            "detected_events":  detected_events[:8],   # top 8
            "compound_signals": compound_signals,
            "category_scores":  category_scores,
            "active_entities":  active_entities,
            "gdelt_tone":       round(gdelt_tone, 3),
            "explanation":      explanation,
            "source":           "geopolitical",
        }

        with self._lock:
            self._cache = result

        logger.info(
            "GeopoliticalAgent: signal=%+.3f | conf=%.2f | risk=%s | "
            "score=%.3f | compounds=%d | entities=%s",
            signal, confidence, risk_level, risk_score,
            len(compound_signals), active_entities[:3],
        )
        return result

    # ── Scoring helpers ───────────────────────────────────────

    @staticmethod
    def _score_category(
        text: str,
        kw_dict: dict[str, float],
        label: str,
    ) -> tuple[float, list[str]]:
        """
        Scan text for all keywords in the category.
        Returns (raw_score, list_of_event_strings).

        Scoring uses square-root dampening: each additional match
        in the same category contributes less than the first.
        This prevents runaway scores from keyword-dense articles.
        """
        import math, re as _re
        hits: list[tuple[str, float]] = []
        for kw, kw_weight in kw_dict.items():
            # Use word-boundary matching to prevent substring false positives
            # e.g. "war" must not match "ransomware", "ban" must not match "bank"
            pattern = r'\b' + _re.escape(kw) + r'\b'
            if _re.search(pattern, text):
                hits.append((kw, abs(kw_weight)))

        if not hits:
            return 0.0, []

        # Sort descending by weight; take first hit at full weight,
        # subsequent hits at sqrt-dampened fraction
        hits.sort(key=lambda x: x[1], reverse=True)
        raw = 0.0
        for i, (kw, w) in enumerate(hits):
            dampen = 1.0 / math.sqrt(i + 1)   # 1.0, 0.71, 0.58, 0.50, ...
            contribution = w * dampen
            # Preserve sign from original dict (calm keywords are negative)
            sign = -1.0 if kw_dict.get(kw, 0) < 0 else 1.0
            raw += sign * contribution

        events = [f"{label}: {kw}" for kw, _ in hits[:3]]  # top 3 per category
        return round(raw, 4), events

    @staticmethod
    def _check_compound(text: str, cs: dict) -> tuple[bool, str]:
        """
        Fire a compound signal if the required number of entities AND
        at least one action keyword all appear in the text.

        cs may optionally include:
          min_entities (int) — minimum distinct entities required (default 1)
          min_actions  (int) — minimum distinct actions required  (default 1)

        Requiring min_entities=2 for nation-state confrontation compounds
        prevents false positives where only one side is mentioned
        (e.g., sanctions on Russia alone should not fire a Russia-NATO compound).
        """
        entities_found = [e for e in cs["entities"]
                          if re.search(r'\b' + re.escape(e) + r'\b', text)]
        actions_found  = [a for a in cs["actions"]
                          if re.search(r'\b' + re.escape(a) + r'\b', text)]
        min_ent = cs.get("min_entities", 1)
        min_act = cs.get("min_actions",  1)
        if len(entities_found) >= min_ent and len(actions_found) >= min_act:
            return True, cs["signal_note"]
        return False, ""

    @staticmethod
    def _entity_multiplier(text: str) -> tuple[float, list[str]]:
        """
        Find which hotspot entities are present and return the
        highest severity multiplier + list of active entities.
        Only applies a multiplier > 1.0; never dampens.
        """
        found: list[tuple[str, float]] = []
        for entity, mult in _HOTSPOT_ENTITIES.items():
            if re.search(r'\b' + re.escape(entity) + r'\b', text):
                found.append((entity, mult))

        if not found:
            return 1.0, []

        found.sort(key=lambda x: x[1], reverse=True)
        top_mult = found[0][1]   # use highest — don't stack multiply
        return top_mult, [e for e, _ in found]

    @staticmethod
    def _map_risk_level(risk_score: float) -> tuple[str, float]:
        """Map composite risk score to (risk_level, signal)."""
        for threshold, level, signal in _RISK_THRESHOLDS:
            if risk_score >= threshold:
                return level, signal
        return "CALM", +0.20

    @staticmethod
    def _compute_confidence(
        has_gdelt:      bool,
        n_events:       int,
        n_compounds:    int,
        entity_mult:    float,
        has_headlines:  bool,
    ) -> float:
        """
        Confidence reflects how much evidence supports the signal,
        not the magnitude of the signal itself.

        Base: 0.20 (almost no data)
        + headlines present:       +0.15
        + GDELT present:           +0.15
        + each event detected:     +0.04 (capped at +0.20)
        + each compound signal:    +0.08 (capped at +0.20)
        + high-severity entity:    +0.05 per 0.1 above 1.0
        """
        conf = 0.20
        if has_headlines:
            conf += 0.15
        if has_gdelt:
            conf += 0.15
        conf += min(0.20, n_events   * 0.04)
        conf += min(0.20, n_compounds * 0.08)
        # Entity severity boost
        if entity_mult > 1.0:
            conf += min(0.10, (entity_mult - 1.0) * 0.20)
        return round(min(1.0, conf), 4)

    @staticmethod
    def _build_explanation(
        risk_level:       str,
        signal:           float,
        risk_score:       float,
        confidence:       float,
        detected_events:  list[str],
        compound_signals: list[str],
        compound_notes:   list[str],
        active_entities:  list[str],
        gdelt_tone:       float,
        has_gdelt:        bool,
    ) -> str:
        """
        Build a human-readable explanation paragraph for the UI
        and for consumption by any downstream LLM reasoning layer.
        """
        lines: list[str] = []

        # Header
        direction_txt = (
            "Risk-off sentiment likely — avoid new longs" if signal < -0.05 else
            "Risk-on / calm environment — mild tailwind" if signal >  0.05 else
            "Neutral geopolitical backdrop"
        )
        lines.append(
            f"Geopolitical Score: {risk_score:+.2f} | "
            f"Risk Level: {risk_level} | {direction_txt}."
        )

        # Compound events (most important)
        if compound_notes:
            lines.append("Key events detected:")
            for note in compound_notes[:3]:
                lines.append(f"  • {note}")

        # Active entities
        if active_entities:
            ent_str = ", ".join(active_entities[:5])
            lines.append(f"Active geopolitical actors: {ent_str}.")

        # GDELT tone
        if has_gdelt and gdelt_tone != 0.0:
            tone_desc = (
                "very negative" if gdelt_tone < -5 else
                "negative"      if gdelt_tone < -2 else
                "slightly negative" if gdelt_tone < 0 else
                "slightly positive" if gdelt_tone < 2 else
                "positive"      if gdelt_tone < 5 else
                "very positive"
            )
            lines.append(
                f"Global news tone (GDELT): {tone_desc} ({gdelt_tone:+.1f})."
            )

        # Market implication
        if signal < -0.40:
            lines.append(
                "Market implication: Elevated macro risk — "
                "confluence threshold raised, long exposure caution advised."
            )
        elif signal < -0.10:
            lines.append(
                "Market implication: Moderate risk environment — "
                "monitor for escalation before adding new positions."
            )
        elif signal > 0.05:
            lines.append(
                "Market implication: Calm geopolitical backdrop — "
                "modest positive bias for risk assets including crypto."
            )

        lines.append(f"Confidence: {confidence:.0%}")
        return "  ".join(lines)

    # ── Data fetchers ─────────────────────────────────────────

    def _fetch_gdelt(self) -> dict:
        """
        Query GDELT DOC API for tone of crypto + geopolitical global news.
        Free, no API key. Queries last 24h. Returns avg_tone and article count.
        """
        import urllib.request, urllib.parse, json as _json

        query = urllib.parse.quote(
            "bitcoin OR ethereum OR cryptocurrency OR "
            "war OR military OR sanctions OR iran OR "
            "oil OR geopolitical OR conflict OR nuclear OR "
            "taiwan OR ukraine OR north korea OR "
            "cyberattack OR cyber attack OR ransomware OR "
            "pandemic OR outbreak OR WHO emergency OR lockdown OR "
            "terror attack OR assassination OR earthquake OR tsunami"
        )
        url = (
            f"https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={query}&mode=artlist&maxrecords=50"
            f"&format=json&timespan=1d"
        )
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "NexusTrader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = _json.loads(resp.read().decode())

        articles = data.get("articles", [])
        if not articles:
            return {}
        tones = [float(a.get("tone", 0)) for a in articles if a.get("tone") is not None]
        return {
            "article_count": len(articles),
            "avg_tone":       round(sum(tones) / len(tones), 3) if tones else 0.0,
        }

    def _fetch_cc_headlines(self) -> list[str]:
        """
        Fetch recent headlines from CryptoCompare covering crypto,
        regulation, macro, and trading categories.  Title + body
        snippet are concatenated so keyword matching has full context.
        """
        import urllib.request, json as _json

        url = (
            "https://min-api.cryptocompare.com/data/v2/news/"
            "?lang=EN&feeds=cointelegraph,coindesk,theblock,decrypt"
            "&categories=Regulation,Blockchain,Trading,Market,Macro"
            "&limit=50"
        )
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "NexusTrader/1.0"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read().decode())

        headlines = []
        for a in data.get("Data", []):
            title = a.get("title", "")
            body  = a.get("body",  "")[:500]   # wider snippet for compound detection
            if title or body:
                headlines.append(f"{title} {body}".strip())

        return headlines

    # ── Public API ────────────────────────────────────────────

    def get_geopolitical_signal(self) -> dict:
        """Return the most recent cached result (thread-safe)."""
        with self._lock:
            if self._cache:
                return dict(self._cache)
        return {
            "signal":           0.0,
            "confidence":       0.0,
            "risk_level":       "NEUTRAL",
            "risk_score":       0.0,
            "signal_direction": "neutral",
            "detected_events":  [],
            "compound_signals": [],
            "explanation":      "Agent initialising — awaiting first data fetch.",
            "stale":            True,
        }


# ── Module-level singleton ────────────────────────────────────
geopolitical_agent: GeopoliticalAgent | None = None
