# ============================================================
# NEXUS TRADER — Pipeline Context  (Phase 5B Wave 1 v3 + Wave 2)
#
# Immutable, validated context object that carries intermediate
# results between processing pipeline steps. Replaces the mutable
# _pipeline_ctx dict.
#
# Design invariants:
#   - Frozen dataclass: no mutation after creation
#   - Each step produces a NEW PipelineContext via with_*() methods
#   - All data flow is explicit: TQS → Filter → Concentration
#   - Wave 2: FailureMode + EdgeValidity added as optional steps
#   - No silent defaults: Optional[T] = None means "not computed"
#   - Decision hashing for replay identity validation
#
# ZERO PySide6 imports.
# ============================================================
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Optional

from core.intraday.scoring.trade_quality_scorer import TQSResult
from core.intraday.filtering.global_trade_filter import FilterResult, FilterStateSnapshot
from core.intraday.scoring.capital_concentration import ConcentrationResult
from core.intraday.protection.failure_mode_protection import FailureModeResult
from core.intraday.monitoring.edge_validity_monitor import EdgeValidityResult


@dataclass(frozen=True)
class PipelineContext:
    """
    Immutable context propagated through the processing pipeline.

    Each Phase 5B step produces a NEW PipelineContext by calling
    the corresponding with_*() factory method. All fields are
    Optional — None means "step was not executed" (module absent
    or pipeline short-circuited by earlier rejection).

    Wave 1: TQS, Filter, Concentration
    Wave 2: FailureMode, EdgeValidity

    INVARIANT: If concentration is present, tqs MUST also be present.
    Concentration requires an explicit TQS score — there is no default.
    """
    # Wave 1
    tqs: Optional[TQSResult] = None
    filter: Optional[FilterResult] = None
    filter_state: Optional[FilterStateSnapshot] = None
    concentration: Optional[ConcentrationResult] = None
    # Wave 2
    failure_mode: Optional[FailureModeResult] = None
    edge_validity: Optional[EdgeValidityResult] = None

    def with_tqs(self, result: TQSResult) -> PipelineContext:
        """Return new context with TQS result attached."""
        if not isinstance(result, TQSResult):
            raise TypeError(f"Expected TQSResult, got {type(result).__name__}")
        return replace(self, tqs=result)

    def with_filter(
        self, result: FilterResult, state: FilterStateSnapshot,
    ) -> PipelineContext:
        """Return new context with filter result and state snapshot."""
        if not isinstance(result, FilterResult):
            raise TypeError(f"Expected FilterResult, got {type(result).__name__}")
        if not isinstance(state, FilterStateSnapshot):
            raise TypeError(f"Expected FilterStateSnapshot, got {type(state).__name__}")
        return replace(self, filter=result, filter_state=state)

    def with_concentration(self, result: ConcentrationResult) -> PipelineContext:
        """Return new context with concentration result.

        PRECONDITION: tqs must already be set. Concentration depends on
        TQS score — there is no silent default.
        """
        if self.tqs is None:
            raise ValueError(
                "Cannot set concentration without TQS: concentration "
                "requires an explicit TQS score. No silent defaults."
            )
        if not isinstance(result, ConcentrationResult):
            raise TypeError(f"Expected ConcentrationResult, got {type(result).__name__}")
        return replace(self, concentration=result)

    def with_failure_mode(self, result: FailureModeResult) -> PipelineContext:
        """Return new context with failure mode result."""
        if not isinstance(result, FailureModeResult):
            raise TypeError(f"Expected FailureModeResult, got {type(result).__name__}")
        return replace(self, failure_mode=result)

    def with_edge_validity(self, result: EdgeValidityResult) -> PipelineContext:
        """Return new context with edge validity result."""
        if not isinstance(result, EdgeValidityResult):
            raise TypeError(f"Expected EdgeValidityResult, got {type(result).__name__}")
        return replace(self, edge_validity=result)

    def to_dict(self) -> dict:
        """Serialise to dict for logging/audit. Only includes computed steps."""
        d: dict = {}
        if self.tqs is not None:
            d["tqs"] = self.tqs.to_dict()
        if self.filter is not None:
            d["filter"] = self.filter.to_dict()
        if self.filter_state is not None:
            d["filter_state"] = self.filter_state.to_dict()
        if self.concentration is not None:
            d["concentration"] = self.concentration.to_dict()
        if self.failure_mode is not None:
            d["failure_mode"] = self.failure_mode.to_dict()
        if self.edge_validity is not None:
            d["edge_validity"] = self.edge_validity.to_dict()
        return d

    def decision_hash(self, decision_dict: dict) -> str:
        """
        Compute a deterministic SHA-256 hash over the full decision path.

        Includes all pipeline step results + the final ExecutionDecision fields.
        Used for replay identity validation: same inputs → same hash.
        """
        payload = {
            "pipeline": self.to_dict(),
            "decision": decision_dict,
        }
        canonical = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── Empty sentinel ──────────────────────────────────────────
EMPTY_CONTEXT = PipelineContext()


def canonical_asset_order(symbols: list) -> list:
    """
    Return symbols in deterministic canonical order.

    Rule: case-insensitive alphabetical sort on symbol string.
    This ensures identical processing order across runs,
    regardless of scanner discovery order or dict iteration order.
    """
    return sorted(symbols, key=lambda s: s.upper())
