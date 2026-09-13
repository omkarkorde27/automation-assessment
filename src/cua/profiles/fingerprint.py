"""Surface fingerprinting -- the drift canary.

A capability is recorded against one tenant and expected to run on its siblings.
That expectation needs a cheap, structural check: has this tenant's version of
the screen diverged from the one the flow was learned on?

The fingerprint hashes the sorted set of `role|name` pairs on a key screen,
filtered to label-like nodes. It deliberately ignores data, position, styling,
and ordering -- so looking up a different member does not read as drift, but a
renamed field or a removed control does.

This is a canary, not a validator. It says "this screen is not what we recorded"
and demotes the capability out of unattended use for review. It does not try to
guess whether the flow still works, because that judgment needs a person.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Literal

from ..perception.model import Observation, normalize_name
from .schema import DriftPolicy, FingerprintSpec, IncludeSpec, KeyScreen


def fingerprint_screen(observation: Observation, include: IncludeSpec) -> str:
    """Structural hash of one screen."""
    pattern = re.compile(include.name_pattern) if include.name_pattern else None
    parts = []
    for node in observation.nodes:
        if not node.visible:
            continue
        if include.roles and node.role not in include.roles:
            continue
        name = node.name
        if pattern and not pattern.search(name or ""):
            continue
        if "collapse_ws" in include.normalize or "casefold" in include.normalize:
            name = normalize_name(name)
        if not name:
            continue
        parts.append(f"{node.role}|{name}")

    canonical = "\n".join(sorted(set(parts)))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


def describe_screen(observation: Observation, include: IncludeSpec) -> list[str]:
    """The members of the fingerprint set, for a human-readable drift diff.

    A verdict that only says "hash differs" is useless in review; a reviewer
    needs to see that `cell|member id` became `cell|account holder #`.
    """
    pattern = re.compile(include.name_pattern) if include.name_pattern else None
    parts = set()
    for node in observation.nodes:
        if not node.visible:
            continue
        if include.roles and node.role not in include.roles:
            continue
        if pattern and not pattern.search(node.name or ""):
            continue
        name = normalize_name(node.name)
        if name:
            parts.add(f"{node.role}|{name}")
    return sorted(parts)


@dataclass
class ScreenDrift:
    screen_id: str
    recorded: str
    observed: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def drifted(self) -> bool:
        return self.recorded != self.observed

    def describe(self) -> str:
        if not self.drifted:
            return f"{self.screen_id}: unchanged"
        bits = []
        if self.removed:
            bits.append(f"-{self.removed}")
        if self.added:
            bits.append(f"+{self.added}")
        return f"{self.screen_id}: drifted {' '.join(bits)}"


@dataclass
class DriftVerdict:
    verdict: Literal["ok", "drifted", "unknown"]
    screens: list[ScreenDrift] = field(default_factory=list)
    action: Literal["none", "demote_to_draft", "warn", "fail"] = "none"

    @property
    def drifted_screens(self) -> list[str]:
        return [s.screen_id for s in self.screens if s.drifted]

    def describe(self) -> str:
        if self.verdict == "unknown":
            return "no recorded fingerprint to compare against"
        if self.verdict == "ok":
            return f"{len(self.screens)} key screen(s) match the recording"
        return (
            f"drift on {self.drifted_screens} -> {self.action}; "
            + "; ".join(s.describe() for s in self.screens if s.drifted)
        )


def compare(
    recorded: dict[str, str],
    observed: dict[str, str],
    *,
    policy: DriftPolicy | None = None,
    recorded_members: dict[str, list[str]] | None = None,
    observed_members: dict[str, list[str]] | None = None,
) -> DriftVerdict:
    """Compare recorded fingerprints against fresh ones.

    Only screens present in BOTH are compared. A screen the run never reached
    is not evidence of drift -- absence of observation is not observation of
    absence, and treating it as drift would demote capabilities for taking a
    shorter path.
    """
    policy = policy or DriftPolicy()
    if not recorded:
        return DriftVerdict(verdict="unknown")

    screens: list[ScreenDrift] = []
    for screen_id, recorded_hash in recorded.items():
        if screen_id not in observed:
            continue
        rec_members = (recorded_members or {}).get(screen_id, [])
        obs_members = (observed_members or {}).get(screen_id, [])
        screens.append(
            ScreenDrift(
                screen_id=screen_id,
                recorded=recorded_hash,
                observed=observed[screen_id],
                added=sorted(set(obs_members) - set(rec_members)),
                removed=sorted(set(rec_members) - set(obs_members)),
            )
        )

    if not screens:
        return DriftVerdict(verdict="unknown")
    if any(s.drifted for s in screens):
        return DriftVerdict(verdict="drifted", screens=screens, action=policy.on_mismatch)
    return DriftVerdict(verdict="ok", screens=screens, action="none")


def screen_for(spec: FingerprintSpec, screen_id: str) -> KeyScreen | None:
    return next((s for s in spec.key_screens if s.id == screen_id), None)
