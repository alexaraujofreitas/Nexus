# ============================================================
# NEXUS TRADER — Intraday Execution Package
#
# Phase 5 execution infrastructure: fills, orders, gates.
# ============================================================

from .fill_simulator import (
    DefaultFeeModel,
    DefaultSlippageModel,
    FeeModel,
    FillSimulator,
    SlippageModel,
)
from .order_manager import OrderManager
from .intraday_executor import IntradayExecutor
from .execution_engine import ExecutionEngine
from .execution_gate import ExecutionGate

__all__ = [
    # Protocols & defaults
    "FeeModel",
    "SlippageModel",
    "DefaultFeeModel",
    "DefaultSlippageModel",
    # Simulators & managers
    "FillSimulator",
    "OrderManager",
    "IntradayExecutor",
    "ExecutionEngine",
    "ExecutionGate",
]
