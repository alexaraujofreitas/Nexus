"""
research/engine/experiment_store.py
======================================
Persistence layer for Research Lab experiments.

Directory layout:
  research/experiments/{experiment_id}/
    config.json     — experiment config (params, assets, dates, commit hash)
    trials.csv      — one row per completed trial
    leaderboard.csv — top-N trials sorted by objective
    summary.json    — aggregate stats
    logs.txt        — append-only trial log
"""
from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

ROOT        = Path(__file__).resolve().parent.parent.parent
EXP_BASE    = ROOT / "research" / "experiments"


@dataclass
class ExperimentConfig:
    experiment_id: str
    created_at:    str
    date_start:    str
    date_end:      str
    symbols:       list
    mode:          str          # "combined" | "pbl_only" | "slc_only"
    objective:     str          # "profit_factor" | "cagr" | "sharpe"
    cost_per_side: float
    fixed_params:  dict
    search_params: dict         # key → {"min", "max", "step"}
    commit_hash:   str = ""
    baseline_pf:   float = 0.0
    notes:         str = ""


@dataclass
class TrialResult:
    trial_id:        int
    params:          dict
    n_trades:        int
    profit_factor:   float
    win_rate:        float
    cagr:            float
    max_drawdown:    float
    pbl_n:           int = 0
    pbl_pf:          float = 0.0
    slc_n:           int = 0
    slc_pf:          float = 0.0
    elapsed_s:       float = 0.0
    status:          str = "ok"   # "ok" | "error"
    error:           str = ""
    timestamp:       str = field(default_factory=lambda: datetime.utcnow().isoformat())


class ExperimentStore:
    """
    Manages one experiment's persistence on disk.
    Thread-safe for append operations (one writer at a time).
    """

    def __init__(self, experiment_id: str):
        self.experiment_id = experiment_id
        self.exp_dir = EXP_BASE / experiment_id
        self.exp_dir.mkdir(parents=True, exist_ok=True)
        self._config_path   = self.exp_dir / "config.json"
        self._trials_path   = self.exp_dir / "trials.csv"
        self._leader_path   = self.exp_dir / "leaderboard.csv"
        self._summary_path  = self.exp_dir / "summary.json"
        self._log_path      = self.exp_dir / "logs.txt"
        self._trial_count   = 0
        self._best_pf       = 0.0
        self._all_results:  list[TrialResult] = []

    # ─────────────────────────────────────────────────────────────────────────

    def save_config(self, config: ExperimentConfig) -> None:
        with open(self._config_path, "w") as f:
            json.dump(asdict(config), f, indent=2)

    def load_config(self) -> Optional[ExperimentConfig]:
        if not self._config_path.exists():
            return None
        with open(self._config_path) as f:
            return ExperimentConfig(**json.load(f))

    def append_trial(self, result: TrialResult) -> None:
        """Append one trial to trials.csv. Creates header on first write."""
        self._all_results.append(result)
        self._trial_count += 1
        if result.profit_factor > self._best_pf:
            self._best_pf = result.profit_factor

        write_header = not self._trials_path.exists()
        row = asdict(result)
        # Flatten params dict into CSV columns (prefix p_)
        params_flat = {f"p_{k.split('.')[-1]}": v for k, v in result.params.items()}
        flat_row = {**{k: v for k, v in row.items() if k != "params"}, **params_flat}

        with open(self._trials_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=flat_row.keys())
            if write_header:
                writer.writeheader()
            writer.writerow(flat_row)

        self._update_leaderboard()
        self._update_summary()

    def _update_leaderboard(self, top_n: int = 20) -> None:
        """Rewrite leaderboard with top-N by PF."""
        sorted_results = sorted(
            [r for r in self._all_results if r.status == "ok"],
            key=lambda r: r.profit_factor,
            reverse=True,
        )[:top_n]
        if not sorted_results:
            return
        rows = [asdict(r) for r in sorted_results]
        for i, row in enumerate(rows):
            row["rank"] = i + 1
        fieldnames = ["rank"] + [k for k in rows[0].keys() if k != "rank"]
        with open(self._leader_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _update_summary(self) -> None:
        """Write a summary JSON with aggregate stats."""
        ok  = [r for r in self._all_results if r.status == "ok"]
        err = [r for r in self._all_results if r.status != "ok"]
        best = max(ok, key=lambda r: r.profit_factor) if ok else None
        summary = {
            "experiment_id":  self.experiment_id,
            "total_trials":   len(self._all_results),
            "ok_trials":      len(ok),
            "error_trials":   len(err),
            "best_pf":        best.profit_factor if best else 0.0,
            "best_params":    best.params if best else {},
            "best_n":         best.n_trades if best else 0,
            "best_wr":        best.win_rate if best else 0.0,
            "best_cagr":      best.cagr if best else 0.0,
            "updated_at":     datetime.utcnow().isoformat(),
        }
        with open(self._summary_path, "w") as f:
            json.dump(summary, f, indent=2)

    def log(self, message: str) -> None:
        """Append a line to the experiment log."""
        ts = datetime.utcnow().strftime("%H:%M:%S")
        with open(self._log_path, "a") as f:
            f.write(f"[{ts}] {message}\n")

    def get_leaderboard(self, top_n: int = 20) -> list[dict]:
        """Return top-N results as list of dicts."""
        ok = [r for r in self._all_results if r.status == "ok"]
        sorted_results = sorted(ok, key=lambda r: r.profit_factor, reverse=True)[:top_n]
        return [asdict(r) for r in sorted_results]

    @classmethod
    def list_experiments(cls) -> list[str]:
        """Return sorted list of existing experiment IDs."""
        if not EXP_BASE.exists():
            return []
        return sorted(
            [d.name for d in EXP_BASE.iterdir() if d.is_dir()],
            reverse=True,
        )

    @classmethod
    def new_id(cls) -> str:
        """Generate a unique experiment ID."""
        return f"exp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
