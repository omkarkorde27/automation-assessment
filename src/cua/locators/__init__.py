"""Locators: how a recorded flow names a control, and how it resolves again."""

from .model import (
    Anchor,
    AnchorRelative,
    BBoxNormalized,
    FrameRef,
    LabelFor,
    LocatorBundle,
    LocatorCandidate,
    RecordedNode,
    RoleNameExact,
    RoleNamePattern,
    RoleOrdinal,
    SectionScope,
    STRATEGY_WEIGHT,
)
from .resolve import (
    LocatorAmbiguous,
    LocatorError,
    LocatorUnresolved,
    FrameUnresolved,
    ResolvedLocator,
    resolve,
    resolve_node,
)

__all__ = [
    "Anchor", "AnchorRelative", "BBoxNormalized", "FrameRef", "LabelFor",
    "LocatorBundle", "LocatorCandidate", "RecordedNode", "RoleNameExact",
    "RoleNamePattern", "RoleOrdinal", "SectionScope", "STRATEGY_WEIGHT",
    "LocatorAmbiguous", "LocatorError", "LocatorUnresolved", "FrameUnresolved",
    "ResolvedLocator", "resolve", "resolve_node",
]
