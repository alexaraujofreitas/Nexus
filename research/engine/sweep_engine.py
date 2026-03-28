"""
research/engine/sweep_engine.py
=================================
Parallel parameter sweep engine for the Research Lab.

Uses multiprocessing.Pool so each trial runs in a separate process with its
own BacktestRunner instance (data loaded once per worker via module-level cache).

Architecture
------------
  SweepEngine.start(config) → spawns coordinator QThread
  Coordinator → multiprocessing.Pool.imap_unordered(worker_run, trials)
  Each result → emit trial_done Signal → UI updates
  Progress → poll via QTimer or coordinator loop

Worker function (module-level, picklable):
  _worker_run(args) → loads BacktestRunner once per process, runs trial
"""
from __future__ import annotations

import itertools
import logging
import multiprocessing as mp
import random
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

# ── Module-level worker cache (one per process) ───────────────────────────────
_worker_runner = None


def _init_worker():
    """Called once per worker process — loads BacktestRunner."""
    global _worker_runner
    try:
        from research.engine.backtest_runner import BacktestRunner
        import logging
        logging.disable(logging.WARNING)   # suppress per-trial noise
        _worker_runner = BacktestRunner(
            date_start="2022-03-22",
            date_end="2026-03-21",
        )
        _worker_runner.load_data()
    except Exception as e:
        _worker_runner = None
        print(f"[worker] init failed: {e}", flush=True)


def _worker_run(args: tuple) -> dict:
    """
    Picklable worker function.
    args = (trial_id, params, cost_per_side)
    Returns metrics dict + trial_id.
    """
    import traceback
    trial_id, params, cost_per_side = args
    global _worker_runner
    if _worker_runner is None:
        return {"trial_id": trial_id, "status": "error", "error": "runner not init", "params": params}
    try:
        result = _worker_runner.run(params, cost_per_side=cost_per_side)
        result["trial_id"] = trial_id
        result["status"]   = "ok"
        # Drop per-trade list to save memory between processes
        result.pop("all_trades", None)
        return result
    except Exception as e:
        return {
            "trial_id": trial_id,
            "status":   "error",
            "error":    str(e),
            "tb":       traceback.format_exc()[:500],
            "params":   params,
        }


# ── Trial generation ──────────────────────────────────────────────────────────

def generate_coarse_grid(param_defs: list, fixed_params: dict) -> list[dict]:
    """
    Generate all combinations of optimize-mode parameters using their
    coarse_values(), combined with fixed_params.
    """
    optimize = [p for p in param_defs if p.mode == "OPTIMIZE"]
    fixed    = {p.settings_key: p.default for p in param_defs if p.mode == "FIXED"}
    fixed.update(fixed_params)

    if not optimize:
        return [fixed]

    keys   = [p.settings_key for p in optimize]
    combos = list(itertools.product(*[p.coarse_values() for p in optimize]))
    trials = []
    for combo in combos:
        trial = dict(fixed)
        for k, v in zip(keys, combo):
            trial[k] = v
        trials.append(trial)
    return trials


def generate_random_trials(
    param_defs: list,
    fixed_params: dict,
    n_trials: int,
    seed: int = 42,
) -> list[dict]:
    """Generate random trials within each parameter's range."""
    rng = random.Random(seed)
    optimize = [p for p in param_defs if p.mode == "OPTIMIZE"]
    fixed    = {p.settings_key: p.default for p in param_defs if p.mode == "FIXED"}
    fixed.update(fixed_params)

    trials = []
    for _ in range(n_trials):
        trial = dict(fixed)
        for p in optimize:
            if p.dtype == "int":
                v = rng.randint(int(p.range_min), int(p.range_max))
            else:
                v = round(rng.uniform(p.range_min, p.range_max), 3)
            trial[p.settings_key] = v
        trials.append(trial)
    return trials


# ── Sweep engine ─────────────────────────────────────────────────────────────

class SweepEngine:
    """
    Runs a parameter sweep using multiprocessing.Pool.

    Usage (non-Qt, e.g. testing):
        engine = SweepEngine(n_workers=2)
        for result in engine.run_sweep(trials, cost_per_side=0.0004):
            print(result["profit_factor"])

    In the Research Lab UI, SweepWorkerThread wraps this in a QThread.
    """

    def __init__(self, n_workers: int = 2):
        self.n_workers   = max(1, min(n_workers, mp.cpu_count()))
        self._cancelled  = False

    def cancel(self):
        self._cancelled = True

    def run_sweep(
        self,
        trials: list[dict],
        cost_per_side: float = 0.0004,
        progress_cb: Optional[Callable] = None,
    ):
        """
        Generator — yields result dicts as they complete.
        progress_cb(completed, total, best_pf)
        """
        self._cancelled = False
        total = len(trials)
        if total == 0:
            return

        args = [(i, params, cost_per_side) for i, params in enumerate(trials)]
        best_pf = 0.0
        completed = 0

        with mp.Pool(
            processes=self.n_workers,
            initializer=_init_worker,
        ) as pool:
            for result in pool.imap_unordered(_worker_run, args, chunksize=1):
                if self._cancelled:
                    pool.terminate()
                    return
                completed += 1
                if result.get("status") == "ok":
                    pf = result.get("profit_factor", 0.0)
                    if pf > best_pf:
                        best_pf = pf
                if progress_cb:
                    progress_cb(completed, total, best_pf)
                yield result

    def run_baseline(self, cost_per_side: float = 0.0004) -> dict:
        """
        Run a single baseline trial (default parameters).
        Returns result dict.
        """
        from research.engine.parameter_registry import default_params
        trials = [default_params()]
        for r in self.run_sweep(trials, cost_per_side):
            return r
        return {}
