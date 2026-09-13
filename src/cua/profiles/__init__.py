"""App profiles: what an application is, and per-tenant overlays."""

from .fingerprint import DriftVerdict, ScreenDrift, compare, describe_screen, fingerprint_screen
from .resolve import (
    ProfileError, ProfileNotFound, ProfileRepository, ResolvedProfile,
    SpecializationReport, specialize,
)
from .schema import AppProfile, AuthSpec, HardFailure, ProfileRecovery, StuckPattern

__all__ = [
    "DriftVerdict", "ScreenDrift", "compare", "describe_screen", "fingerprint_screen",
    "ProfileError", "ProfileNotFound", "ProfileRepository", "ResolvedProfile",
    "SpecializationReport", "specialize", "AppProfile", "AuthSpec", "HardFailure",
    "ProfileRecovery", "StuckPattern",
]
