"""Escalation: filing human work, and handing a live session over.

Deliberately shallow. `console` is NOT re-exported here -- it pulls in FastAPI,
and `replay/engine.py` imports this package on every run. The engine needs the
request shape and the broker; it has no business dragging a web framework into
a deterministic executor.
"""

from .broker import HUMAN_ACTIONS, InterventionBroker, LiveSession, UnknownNode
from .requests import (
    Disposition,
    InterventionRequest,
    InterventionStatus,
    InterventionStore,
    Resolution,
)

__all__ = [
    "Disposition", "HUMAN_ACTIONS", "InterventionBroker", "InterventionRequest",
    "InterventionStatus", "InterventionStore", "LiveSession", "Resolution", "UnknownNode",
]
