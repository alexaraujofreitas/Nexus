"""
NEXUS TRADER — Multi-Agent Intelligence Layer

Each agent is an autonomous QThread that collects a specific
category of market intelligence and publishes normalised signals
onto the EventBus.

Signal contract:
  Every agent publishes a dict that always contains:
    signal      : float  — normalised [-1.0, +1.0]
                           -1 = strong bearish / high risk
                           +1 = strong bullish / low risk
                            0 = neutral
    confidence  : float  — [0.0, 1.0] — how reliable is this signal right now
    source      : str    — agent name
    updated_at  : str    — ISO timestamp of last data fetch
    stale       : bool   — True if data older than max_staleness
  Plus agent-specific keys.

Agents degrade gracefully: if their data source is unavailable,
they publish signal=0, confidence=0, stale=True and continue
polling until the source recovers.
"""
from core.agents.base_agent import BaseAgent

__all__ = ["BaseAgent"]
