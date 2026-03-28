"""
research/engine/baseline_registry.py
=======================================
Baseline lock: stores expected metrics + tolerance bands for the canonical
NexusTrader PBL/SLC backtest.

On each Research Lab run the baseline is re-checked. If the locked baseline
cannot be reproduced within tolerance, ALL optimization is blocked.

Registry file: research/engine/baseline_registry.json
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

ROOT         = Path(__file__).resolve().parent.parent.parent
REGISTRY_PATH = ROOT / "research" / "engine" / "baseline_registry.json"


@dataclass
class BaselineEntry:
    name:               str
    locked_at:          str   # ISO date
    commit_hash:        str
    date_start:         str
    date_end:           str
    symbols:            list

    # Metrics (zero-fee scenario)
    n_trades:           int
    profit_factor:      float   # zero fees
    profit_factor_fees: float   # 0.04%/side
    win_rate:           float
    cagr:               float   # zero fees

    # Tolerances (inclusive bands for pass/fail)
    tol_n_min:          int
    tol_n_max:          int
    tol_pf_min:         float
    tol_pf_max:         float
    tol_pf_fees_min:    float
    tol_pf_fees_max:    float

    dataset_fingerprints: dict = None  # sha256[:16] per parquet file

    def check(self, result_zerofee: dict, result_fees: dict) -> tuple[bool, list[str]]:
        """
        Validate run results against this baseline entry.
        Returns (passed, list_of_failures).
        """
        failures = []
        n = result_zerofee.get("n_trades", 0)
        pf = result_zerofee.get("profit_factor", 0.0)
        pf_f = result_fees.get("profit_factor", 0.0)

        if not (self.tol_n_min <= n <= self.tol_n_max):
            failures.append(
                f"n_trades={n} outside [{self.tol_n_min}, {self.tol_n_max}]"
            )
        if not (self.tol_pf_min <= pf <= self.tol_pf_max):
            failures.append(
                f"PF(zero)={pf:.4f} outside [{self.tol_pf_min}, {self.tol_pf_max}]"
            )
        if not (self.tol_pf_fees_min <= pf_f <= self.tol_pf_fees_max):
            failures.append(
                f"PF(fees)={pf_f:.4f} outside [{self.tol_pf_fees_min}, {self.tol_pf_fees_max}]"
            )
        return len(failures) == 0, failures


# ── Default locked baseline (established Session 38, 2026-03-28) ──────────────

_DEFAULT_BASELINE = BaselineEntry(
    name               = "v9_canonical_2022-03-22_2026-03-21",
    locked_at          = "2026-03-28",
    commit_hash        = "90b94b1",
    date_start         = "2022-03-22",
    date_end           = "2026-03-21",
    symbols            = ["BTC/USDT", "SOL/USDT", "ETH/USDT"],
    n_trades           = 1731,
    profit_factor      = 1.3797,
    profit_factor_fees = 1.2756,
    win_rate           = 0.561,
    cagr               = 0.675,
    tol_n_min          = 1700,
    tol_n_max          = 1760,
    tol_pf_min         = 1.35,
    tol_pf_max         = 1.41,
    tol_pf_fees_min    = 1.25,
    tol_pf_fees_max    = 1.31,
    dataset_fingerprints = {},
)


def load_baseline() -> BaselineEntry:
    """Load baseline from JSON file, falling back to built-in default."""
    if REGISTRY_PATH.exists():
        try:
            with open(REGISTRY_PATH) as f:
                data = json.load(f)
            return BaselineEntry(**data)
        except Exception as e:
            logger.warning("Could not load baseline registry: %s — using default", e)
    return _DEFAULT_BASELINE


def save_baseline(entry: BaselineEntry) -> None:
    """Write baseline to JSON file."""
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    d = asdict(entry)
    with open(REGISTRY_PATH, "w") as f:
        json.dump(d, f, indent=2)
    logger.info("Baseline registry saved: %s", REGISTRY_PATH)


def lock_from_run(
    result_zerofee: dict,
    result_fees:    dict,
    commit_hash:    str = "90b94b1",
    fingerprints:   Optional[dict] = None,
) -> BaselineEntry:
    """
    Create a new baseline entry from two fresh run results and save it.
    Used when establishing a new canonical baseline.
    """
    from datetime import date
    n  = result_zerofee["n_trades"]
    pf = result_zerofee["profit_factor"]
    pf_f = result_fees["profit_factor"]
    wr = result_zerofee["win_rate"]
    cagr = result_zerofee["cagr"]

    # Tolerance bands: ±2% on PF, ±1.5% on n
    entry = BaselineEntry(
        name               = f"canonical_{date.today().isoformat()}",
        locked_at          = date.today().isoformat(),
        commit_hash        = commit_hash,
        date_start         = "2022-03-22",
        date_end           = "2026-03-21",
        symbols            = ["BTC/USDT", "SOL/USDT", "ETH/USDT"],
        n_trades           = n,
        profit_factor      = pf,
        profit_factor_fees = pf_f,
        win_rate           = wr,
        cagr               = cagr,
        tol_n_min          = int(n * 0.985),
        tol_n_max          = int(n * 1.015),
        tol_pf_min         = round(pf * 0.980, 4),
        tol_pf_max         = round(pf * 1.020, 4),
        tol_pf_fees_min    = round(pf_f * 0.980, 4),
        tol_pf_fees_max    = round(pf_f * 1.020, 4),
        dataset_fingerprints = fingerprints or {},
    )
    save_baseline(entry)
    return entry
