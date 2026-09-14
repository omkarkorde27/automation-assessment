"""What happened during a discovery run.

The transcript is the recorder's only input, which is the point: the recorder
never sees the model's messages. It sees *what was done, to which node, and
why* -- so the artifact is derived from observed behaviour rather than from
prose the model wrote about its behaviour.

That separation is also what keeps the transcript safe to persist. It holds node
descriptors and the model's stated reasons; it never holds raw screen text, and
values are marked at capture time so a recorded step can say "a five-digit
number went here" without saying which one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..perception.model import Observation, UiNode
from ..surfaces.base import ActionType, RiskTier


def value_shape(value: str) -> str:
    """A regex describing a value, never the value.

    Artifacts record shapes so a reviewer can see what kind of thing a field
    takes without the artifact becoming a place customer data lives.
    """
    if not value:
        return ""
    if re.fullmatch(r"\d+", value):
        return rf"^\d{{{len(value)}}}$"
    if re.fullmatch(r"[\d,]*\.?\d*", value):
        return r"^\d+(\.\d{2})?$"
    if re.fullmatch(r"[A-Z0-9]{2,6}", value):
        return r"^[A-Z0-9]{2,6}$"
    if "@" in value:
        return r"^[^@]+@[^@]+$"
    return rf"^.{{{max(1, len(value) - 2)},{len(value) + 2}}}$"


@dataclass
class DiscoveryStep:
    """One tool call the model made, and what the surface did about it."""

    index: int
    tool: str
    reason: str
    """The model's own justification. Required by every tool schema, which is
    how "a log of what the agent did and why" is structural rather than a
    separate thing somebody has to remember to write."""

    action_type: ActionType | None = None
    node: UiNode | None = None
    """The node the model selected, as it was at selection time. The recorder
    derives a LocatorBundle from this -- the model never writes a selector."""

    value: str | None = None
    is_param_candidate: bool = False
    param_name: str = ""
    url: str | None = None
    risk: RiskTier = RiskTier.READ_ONLY

    ok: bool = True
    error: str = ""
    failure_class: str = ""

    pre: Observation | None = None
    post: Observation | None = None

    pruned: bool = False
    pruned_because: str = ""

    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def changed_screen(self) -> bool:
        if self.pre is None or self.post is None:
            return False
        return self.pre.state_hash() != self.post.state_hash()

    def summary(self) -> str:
        bits = [f"{self.index:02d} {self.tool}"]
        if self.node is not None:
            bits.append(self.node.describe())
        if self.url:
            bits.append(self.url)
        if not self.ok:
            bits.append(f"FAILED: {self.error}")
        return " | ".join(bits)


@dataclass
class DeclaredOutcome:
    """A business outcome the model recognized while exploring."""

    code: str
    hint: str
    at_step: int
    observed_node: UiNode | None = None


@dataclass
class DeclaredExtraction:
    """A value the model identified as an answer to the goal."""

    output_name: str
    node: UiNode
    at_step: int
    reason: str = ""


@dataclass
class DiscoveryRun:
    """The whole record of one goal being figured out."""

    run_id: str
    goal: str
    tenant: str
    base_url: str
    model: str
    profile_ref: str = ""
    profile_hash: str = ""

    steps: list[DiscoveryStep] = field(default_factory=list)
    outcomes: list[DeclaredOutcome] = field(default_factory=list)
    extractions: list[DeclaredExtraction] = field(default_factory=list)

    status: str = "running"
    """running | finished | gave_up | aborted | budget_exhausted | stuck | denied"""

    stop_reason: str = ""
    finish_reason: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at: datetime | None = None

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def succeeded(self) -> bool:
        return self.status == "finished"

    @property
    def kept_steps(self) -> list[DiscoveryStep]:
        """The successful path -- what the recorder turns into a flow."""
        return [s for s in self.steps if not s.pruned]

    def params_used(self) -> dict[str, str]:
        """Values the model marked as parameters, by name.

        R-M4-1 compares literals against this map, so it must contain every
        value the run typed on the operator's behalf.
        """
        return {s.param_name: s.value for s in self.steps
                if s.is_param_candidate and s.param_name and s.value}

    def screen_texts(self) -> set[str]:
        """Every accessible name the app displayed during the run.

        R-M4-1 refuses to bake any of these into a literal: a value read off the
        screen and typed back is recorded customer data, not a constant.
        """
        seen: set[str] = set()
        for step in self.steps:
            for obs in (step.pre, step.post):
                if obs is None:
                    continue
                for node in obs.nodes:
                    if node.name and not node.sensitive:
                        seen.add(node.name.strip())
                    if node.value and not node.sensitive:
                        seen.add(node.value.strip())
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "tenant": self.tenant,
            "model": self.model,
            "profile": self.profile_ref,
            "profile_hash": self.profile_hash,
            "status": self.status,
            "stop_reason": self.stop_reason,
            "finish_reason": self.finish_reason,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "tokens": {"input": self.input_tokens, "output": self.output_tokens},
            "steps": [
                {
                    "index": s.index, "tool": s.tool, "reason": s.reason,
                    "action": s.action_type.value if s.action_type else None,
                    "node": s.node.describe() if s.node else None,
                    "value_shape": value_shape(s.value) if s.value else None,
                    "param": s.param_name or None,
                    "url": s.url, "risk": s.risk.value,
                    "ok": s.ok, "error": s.error or None,
                    "changed_screen": s.changed_screen,
                    "pruned": s.pruned, "pruned_because": s.pruned_because or None,
                }
                for s in self.steps
            ],
            "outcomes": [{"code": o.code, "hint": o.hint, "at_step": o.at_step}
                         for o in self.outcomes],
            "extractions": [{"output": e.output_name, "node": e.node.describe(),
                             "at_step": e.at_step} for e in self.extractions],
        }
